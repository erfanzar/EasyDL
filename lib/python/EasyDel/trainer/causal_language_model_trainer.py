import copy
import dataclasses
import threading
import warnings
from glob import glob
import os
import sys
import time
import typing

import IPython.display
from flax.core import FrozenDict
import termcolor
from fjformer.func.loss_func import (
    cross_entropy_loss_and_accuracy,
    SpecialLossNormalizingFactor,
    get_loss_normalizing_factor_and_weights,
    compute_weighted_cross_entropy_and_accuracy,
)
import wandb

import jax
import flax
from tqdm import tqdm
from ..utils.utils import prefix_print
from ..smi import initialise_tracking, get_mem, get_capacity_matrix
from jax.experimental.pjit import pjit
from jax.sharding import PartitionSpec
from jax import numpy as jnp
from fjformer import (
    match_partition_rules,
    make_shard_and_gather_fns,
    CheckpointManager,
    with_sharding_constraint
)
from ..etils.errors import EasyDelTimerError
from typing import Any, Optional, Tuple, Callable, Mapping
from ..etils.easystate import EasyDelState
from .base_trainer import BaseTrainer, TrainerConfigureFunctionFuncOutput


def create_casual_language_model_train_step(
        partition_spec=PartitionSpec(("dp", "fsdp"), "sp"),
        label_smoothing_factor=0.0,
        z_loss=0.0,
        gradient_accumulation_steps: int = 1,
):
    """
    The create_casual_language_model_train_step function is a training step function that takes in the current state
    of the model,and a batch of data. It then calculates the loss and accuracy for this batch, and returns
    an updated state with new parameters based on these gradients.

    :param partition_spec: Specify which devices the model will be split across
    :param label_smoothing_factor: A float in [0, 1] specifying the amount of label smoothing to apply,
           where 0 means no smoothing.
    :param z_loss: A regularization term that adds a penalty for large weights, where 0 means no regularization.
    :param gradient_accumulation_steps: int : gradient accumulation step size from arguments
    :return: A casual_language_model_train_step function that takes in the current state of the model,
    """
    assert gradient_accumulation_steps > 0, "gradient_accumulation_steps must be greater than 0"  # Ignore

    def casual_language_model_train_step(state, batch):
        """
        The casual_language_model_train_step function is a training step function that takes in the current state
        of the model and a batch of data. It then calculates the loss and accuracy for this batch,
        and returns an updated state with new parameters based on these gradients.

        :param state: Store the model parameters
        :param batch: Pass the data to the model, dict with
                      input_ids(bs, seq_len), labels(bs, seq_len-1), attention_mask(bs, seq_len)
        :return: A tuple of (state, loss, accuracy)
        """
        batch = with_sharding_constraint(batch, partition_spec)

        def calculate_loss(params):
            labels = batch.pop("labels")  # already shifted left
            model_outputs = state.apply_fn(params=params, **batch, return_dict=True)
            logits = model_outputs.logits
            aux_loss = getattr(model_outputs, "aux_loss", None)
            loss_normalizing_factor = (
                SpecialLossNormalizingFactor.NUM_REAL_TARGET_TOKENS
            )
            # loss_weights is 1 unless the label is <= 0 or the attention mask is 0
            loss_weights = jnp.where(
                (batch["attention_mask"][:, 1:] != 0) & (labels > 0), 1, 0
            )
            lnf, weights = get_loss_normalizing_factor_and_weights(
                loss_normalizing_factor,
                {
                    "decoder_target_tokens": labels,
                    "decoder_loss_weights": loss_weights,
                },
            )
            (
                loss,
                z_loss_computed,
                weight_sum,
                accuracy,
            ) = compute_weighted_cross_entropy_and_accuracy(
                logits=logits[:, :-1, :],
                targets=labels,
                weights=weights,
                label_smoothing=label_smoothing_factor,
                z_loss=z_loss,
                loss_normalizing_factor=lnf,
            )
            if aux_loss is not None:
                loss += aux_loss
            return loss, (accuracy, z_loss_computed)

        grad_fn = jax.value_and_grad(calculate_loss, has_aux=True)
        (loss__, (accuracy__, z_loss_computed__)), grad = grad_fn(state.params)
        state = state.apply_gradients(grads=grad)

        grad_norms = jax.tree_map(jnp.linalg.norm, grad)
        max_grad_norm = jax.tree_util.tree_reduce(jnp.maximum, grad_norms)
        mean_grad_norm = jax.tree_util.tree_reduce(
            jnp.add, jax.tree_map(jnp.sum, grad_norms)
        ) / jax.tree_util.tree_reduce(jnp.add, jax.tree_map(jnp.size, grad_norms))

        metrics = {
            "accuracy": accuracy__,
            "regularization_z_loss": z_loss_computed__,
            "max_grad_norm": max_grad_norm,
            "mean_grad_norm": mean_grad_norm,
            "grad_norms": grad_norms,
        }

        return state, loss__, metrics

    return casual_language_model_train_step


def create_casual_language_model_evaluation_step(partition_spec=PartitionSpec(("dp", "fsdp"), "sp")):
    """
    The create_casual_language_model_evaluation_step function is used to create a function that calculates the loss
     and accuracy of a model. It takes in a set of parameters, which are then passed into the state.apply_fn function
    to generate logits for each token in the batch. The cross entropy loss and accuracy are then calculated from these
    logits.

    :param partition_spec: Specify the partitioning of the model parameters
    :return: A function that can be used to calculate the loss and accuracy of a model

    """

    def casual_language_model_evaluation_step(state, batch_eval):
        """
        The casual_language_model_evaluation_step function is used to calculate the loss and accuracy of a model.
        It takes in a set of parameters, which are then passed into the state.apply_fn function
        to generate logits for each token in the batch. The cross entropy loss and accuracy are then calculated from
        these logits.

        :param state: Store the model parameters and other information about the training process
        :param batch_eval: Pass the batch of data to the function
        :return: The loss and accuracy of the model

        """
        batch_eval = with_sharding_constraint(batch_eval, partition_spec)

        def calculate_loss(params):
            """
            The calculate_loss function is used to calculate the loss and accuracy of a model.
            It takes in a set of parameters, which are then passed into the state.apply_fn function
            to generate logits for each token in the batch. The cross entropy loss and accuracy are then calculated
            from these logits.

            :param params: Pass the model parameters to the function
            :return: The loss and the accuracy

            """
            labels = batch_eval.pop("labels")
            model_outputs = state.apply_fn(params=params, **batch_eval, return_dict=True)
            logits = model_outputs.logits
            aux_loss = getattr(model_outputs, "aux_loss", None)
            valid = jnp.where(
                (batch_eval["attention_mask"][:, 1:].astype(jnp.float32) != 0)
                & (labels > 0),
                1.0,
                0.0,
            )
            loss, accuracy = cross_entropy_loss_and_accuracy(
                logits[:, :-1, :],
                labels,
                valid,
            )
            if aux_loss is not None:
                loss += aux_loss
            return loss, accuracy

        loss__, accuracy__ = calculate_loss(state.params)
        return loss__, accuracy__

    return casual_language_model_evaluation_step


@dataclasses.dataclass
class TrainerOutput:
    state: EasyDelState
    mesh: Optional[jax.sharding.Mesh]
    checkpoint_manager: Any
    gather_fns: Optional[Any | Mapping[str, Callable] | dict[Callable]] = None
    shard_fns: Optional[Any | Mapping[str, Callable] | dict[Callable]] = None
    last_save_file_name: Optional[str] = None
    checkpoint_path: Optional[str] = None


class CausalLanguageModelTrainer(BaseTrainer):

    def create_collate_function(
            self,
            max_sequence_length: int,
            truncation_mode: typing.Literal["keep_end", "keep_start"] = "keep_end",
    ) -> Callable:
        def collate_fn(batch):
            results = {}
            corrected_sequence = None
            for key in batch[0].keys():
                if truncation_mode == "keep_end":
                    corrected_sequence = [
                        jnp.array(f[key])[..., -max_sequence_length:] for f in batch
                    ]
                else:
                    corrected_sequence = [
                        jnp.array(f[key])[..., :max_sequence_length] for f in batch
                    ]
                results[key] = jnp.stack(corrected_sequence).reshape(
                    -1,
                    corrected_sequence[0].shape[-1]
                )
            return results

        return collate_fn

    def configure_functions(self) -> TrainerConfigureFunctionFuncOutput:
        """
        The configure_functions function is responsible for configuring the functions that will be used in training.
        It does this by first defining a function called function_configurations, which initializes the model parameters and returns
        them as a EasyDelState object. The EasyDelState object contains all the information needed to train or evaluate
        on a batch of data, including:
        :param self: Access the class attributes
        :return: A TrainerConfigureFunctionFuncOutput object

        """

        def initialize_state_function():
            initialized_parameters = self.model.init_weights(
                jax.random.PRNGKey(0),
                self.arguments.init_input_shape
            )

            if self.arguments.dtype == jnp.bfloat16:
                initialized_parameters = self.model.to_bf16(initialized_parameters)
            elif self.arguments.dtype == jnp.float16:
                initialized_parameters = self.model.to_fp16(initialized_parameters)

            tx = self.tx
            parameters = flax.core.freeze({"params": initialized_parameters})
            tx_init = copy.deepcopy(self.arguments.optimizer_kwargs)

            if self.rapture is not None:
                lora_parameters = self.lora_parameters
                if self.arguments.dtype == jnp.bfloat16:
                    lora_parameters = self.model.to_bf16(lora_parameters)
                elif self.arguments.dtype == jnp.float16:
                    lora_parameters = self.model.to_fp16(lora_parameters)

                return EasyDelState(
                    step=0,
                    apply_fn=self.lora_apply_fn,
                    params=lora_parameters,
                    tx=self.lora_tx,
                    opt_state=self.lora_opt_state,
                    tx_init=EasyDelState.safe_dict(tx_init),
                    hyperparameters=EasyDelState.create_hyperparameters(self.model.config.model_type),
                    module=self.lora_model,
                    module_config=self.model.config,
                    module_config_args=None,
                )
            else:
                return EasyDelState.create(
                    tx=tx,
                    params=parameters,
                    apply_fn=self.model.__call__,
                    module_config=copy.deepcopy(self.model.config),
                    tx_init=tx_init,
                    hyperparameters=EasyDelState.create_hyperparameters(self.model.config.model_type),
                    module=self.model,
                    module_config_args=None
                )

        def create_state_from_params_function(parameters):
            if self.rapture is None:
                return EasyDelState.create(
                    tx=self.tx,
                    params=parameters,
                    apply_fn=self.model.__call__,
                    module_config=copy.deepcopy(self.model.config),
                    tx_init=copy.deepcopy(self.arguments.optimizer_kwargs),
                    hyperparameters=EasyDelState.create_hyperparameters(self.model.config.model_type),
                    module=self.model,
                    module_config_args=None
                )
            else:
                return EasyDelState(
                    step=0,
                    apply_fn=self.lora_apply_fn,
                    params=parameters,
                    tx=self.lora_tx,
                    opt_state=self.lora_opt_state,
                    tx_init=EasyDelState.safe_dict(copy.deepcopy(self.arguments.optimizer_kwargs)),
                    hyperparameters=EasyDelState.create_hyperparameters(self.model.config.model_type),
                    module=self.lora_model,
                    module_config=self.model.config,
                    module_config_args=None,
                )

        state_shape = jax.eval_shape(initialize_state_function)
        state_partition_spec = match_partition_rules(
            self.config.get_partition_rules(
                fully_sharded_data_parallel=self.arguments.fully_sharded_data_parallel
            ) if self.arguments.custom_rule is None else self.arguments.custom_rule,
            state_shape
        )
        create_sharded_state_from_params_function = pjit(
            create_state_from_params_function,
            in_shardings=(state_partition_spec.params,),
            out_shardings=state_partition_spec,
            donate_argnums=(0,)
        )
        sharded_train_step_function = pjit(
            create_casual_language_model_train_step(
                partition_spec=self.arguments.step_partition_spec,
                label_smoothing_factor=self.arguments.label_smoothing_factor,
                z_loss=self.arguments.z_loss,
            ),
            in_shardings=(state_partition_spec, PartitionSpec()),
            out_shardings=(state_partition_spec, PartitionSpec(), PartitionSpec()),
            donate_argnums=(0, 0),
        )

        sharded_eval_step_function = pjit(
            create_casual_language_model_evaluation_step(self.arguments.step_partition_spec),
            in_shardings=(state_partition_spec, PartitionSpec()),
            out_shardings=(PartitionSpec(), PartitionSpec()),
            donate_argnums=(0, 0),
        )

        mesh = self.arguments.get_mesh()
        self.arguments.ckpt_path_exists()
        checkpoint_manager = self.arguments.get_streaming_checkpointer()
        self.state_partition_spec = state_partition_spec
        self.state_shape = state_shape

        return TrainerConfigureFunctionFuncOutput(
            create_sharded_state_from_params_function=create_sharded_state_from_params_function,
            sharded_train_step_function=sharded_train_step_function,
            sharded_eval_step_function=sharded_eval_step_function,
            mesh=mesh,
            checkpoint_manager=checkpoint_manager,
            initialize_state_function=initialize_state_function
        )

    def initialize_state(
            self,
            model_parameters: Optional[flax.core.FrozenDict] = None,
            state: Optional[EasyDelState] = None,
    ) -> Tuple[EasyDelState, Mapping[str, Callable], Mapping[str, Callable]]:
        if model_parameters is None and state is None and self.rapture is None and self.checkpoint_path is None:
            raise RuntimeError(
                "You are passing `model_parameters=None`, `state=None`, and `checkpoint_path=None` and also you are not"
                " using LoRA, if you are "
                "Using LoRA make sure to pass parameters and Rapture Config correctly otherwise pass the "
                "model_parameters or state."
            )
        if model_parameters is None and state is None:
            model_parameters = self.lora_parameters
        with self.mesh:
            shard_fns, gather_fns = make_shard_and_gather_fns(
                self.state_partition_spec,
                dtype_specs=self.dtype
            )
            if state is not None:
                sharded_state = state
                params = sharded_state.params if not self.arguments.do_shard_fns else jax.tree_util.tree_map(
                    lambda f, x: f(x),
                    shard_fns.params,
                    sharded_state.params
                )
                sharded_state.params = params
                if sharded_state.opt_state is None:
                    prefix_print(
                        "Action", "Optimizer State is not Found!, initializing one."
                    )
                    with jax.default_device(self.arguments.offload_device):
                        sharded_state = sharded_state.init_opt_state()
                        opt_state = sharded_state.opt_state if not self.arguments.do_shard_fns else jax.tree_util.tree_map(
                            lambda f, x: f(x),
                            shard_fns.opt_state,
                            sharded_state.opt_state
                        )
                        sharded_state = sharded_state.replace(
                            opt_state=opt_state
                        )
            elif self.finetune:

                if model_parameters is None and self.checkpoint_path is not None:
                    prefix_print(
                        "Action", f"Loading Model From {self.checkpoint_path}"
                    )
                    with jax.default_device(self.arguments.offload_device):
                        sharded_state = EasyDelState.load_state(
                            verbose=self.arguments.verbose,
                            state_shard_fns=shard_fns,
                            init_optimizer_state=True,
                            checkpoint_path=self.checkpoint_path,
                        )
                        state_shape = jax.eval_shape(lambda: sharded_state)
                        state_partition_spec = match_partition_rules(
                            self.config.get_partition_rules(
                                fully_sharded_data_parallel=self.arguments.fully_sharded_data_parallel
                            ) if self.arguments.custom_rule is None else self.arguments.custom_rule,
                            state_shape
                        )
                        sharded_train_step_function = pjit(
                            create_casual_language_model_train_step(
                                partition_spec=self.arguments.step_partition_spec,
                                label_smoothing_factor=self.arguments.label_smoothing_factor,
                                z_loss=self.arguments.z_loss,
                            ),
                            in_shardings=(state_partition_spec, PartitionSpec()),
                            out_shardings=(state_partition_spec, PartitionSpec(), PartitionSpec()),
                            donate_argnums=(0, 0),
                        )

                        sharded_eval_step_function = pjit(
                            create_casual_language_model_evaluation_step(self.arguments.step_partition_spec),
                            in_shardings=(state_partition_spec, PartitionSpec()),
                            out_shardings=(PartitionSpec(), PartitionSpec()),
                            donate_argnums=(0, 0),
                        )

                        self.state_partition_spec = state_partition_spec
                        self.state_shape = state_shape
                        self.sharded_train_step_function = sharded_train_step_function
                        self.sharded_eval_step_function = sharded_eval_step_function

                    if self.arguments.remove_ckpt_after_load:
                        os.remove(self.checkpoint_path)
                elif model_parameters is not None and self.checkpoint_path is None:
                    prefix_print(
                        "Action", f"Sharding Passed Parameters"
                    )
                    from flax.core import unfreeze
                    if not isinstance(model_parameters, flax.core.FrozenDict):
                        prefix_print(
                            "Warning",
                            "Model Parameters should be like FrozenDict({'params': params}) make sure to "
                            "pass as type FrozenDict in case of not getting UnExcepted Errors "
                        )

                    model_parameters = model_parameters if not self.arguments.do_shard_fns else jax.tree_util.tree_map(
                        lambda f, x: f(x),
                        shard_fns.params,
                        model_parameters,
                    )
                    sharded_state = self.create_sharded_state_from_params_function(model_parameters)
                elif model_parameters is not None and self.checkpoint_path is not None:
                    raise EasyDelTimerError(
                        "You can't pass `model_parameters` and `checkpoint_path` at same time"
                    )
                else:
                    raise EasyDelTimerError(
                        "You should pass `model_parameters` or `checkpoint_path` to trainer in order to load model"
                    )
            else:
                sharded_state = self.initialize_state_function()
                params = sharded_state.params if not self.arguments.do_shard_fns else jax.tree_util.tree_map(
                    lambda f, x: f(x),
                    shard_fns.params,
                    sharded_state.params
                )
                sharded_state.params = params

            self.sharded_state = sharded_state
            return sharded_state, shard_fns, gather_fns

    def _save_state(
            self,
            state: EasyDelState,
            gather_fns: Optional[Any | Mapping[str, Callable] | dict[Callable]],
            milestone: bool = False
    ) -> str:
        step = int(
            jax.device_get(
                state.step
            )
        ) + self.arguments.step_start_point if self.arguments.step_start_point is not None else int(
            jax.device_get(
                state.step
            )
        )

        checkpoint_dir = os.path.join(self.arguments.save_dir, self.arguments.model_name)
        filename_extension = ".easy"
        if self.arguments.save_total_limit:
            checkpoint_files = glob(os.path.join(checkpoint_dir, f"*{filename_extension}"))
            checkpoint_files.sort(key=os.path.getmtime)
            for old_checkpoint in checkpoint_files[:-self.arguments.save_total_limit]:
                os.remove(old_checkpoint)
                termcolor.cprint(f"Removed old checkpoint: {old_checkpoint}", color="red", force_color=True)

        checkpoint_name = f"{self.arguments.model_name}-S{step}"
        filename = f"{checkpoint_name}_{step}" if milestone else f"{checkpoint_name}"
        filename += ".easy"
        termcolor.cprint(f"Saving Model {filename}.", color="cyan", force_color=True)
        state.save_state(
            filename=filename,
            checkpoint_dir=checkpoint_dir,
            gather_fns=gather_fns,
            float_dtype=self.dtype,
            verbose=self.arguments.verbose,
            save_optimizer=self.arguments.save_optimizer_state,
        )
        return filename

    def _start_capturing_memory(self, dir_prefix: str = "/dev/shm" if sys.platform != "win32" else "."):
        def _start():
            while True:
                information_queries = {}
                for key in ["Used", "Usage Percent"]:
                    for device, info in get_capacity_matrix(dir_prefix=dir_prefix).items():
                        information_queries[f"accelerators/{device.replace('_', ' ')} ({key})"] = float(
                            info[key].replace("%", "").replace("GB", "")
                        )
                self.wandb_runtime.log(
                    information_queries
                )
                if self.arguments.stop_capturing_memory:
                    break

        return threading.Thread(target=_start)

    def train(
            self,
            model_parameters: Optional[flax.core.FrozenDict] = None,
            state: Optional[EasyDelState] = None
    ) -> TrainerOutput:
        """
        The train function is the main function of this module.
        It takes a model_parameters argument which can be used to load a pretrained model and finetune it.
        The train function returns an TrainerOutput object that contains the last saved file name, predict func,
        train state, mesh and checkpoint streamer.


        :param self: Make the class methods aware of other methods and attributes within the class
        :param model_parameters: flax.core.FrozenDict: Load a pre-trained model
        :param state: Optional[EasyDelState]: Ready to Use State
        :return: An object of type "TrainerOutput"

        """

        def get_layer_names(frozen_dict, prefix=""):
            layer_names = {}
            for key, value in frozen_dict.items():
                if isinstance(value, FrozenDict):
                    layer_names.update(get_layer_names(value, prefix=f"{prefix}_{key}"))
                else:
                    layer_name = f"{prefix}_{key}".lstrip("/")
                    layer_names[layer_name] = value
            return layer_names

        def count_model_parameters(_p):
            termcolor.cprint(
                f"Model Contain {sum(n.size for n in jax.tree_util.tree_flatten(flax.core.unfreeze(_p))[0]) / 1e9} "
                f"Billion Parameters",
                color="red", force_color=True
            )

        dir_prefix: str = "/dev/shm" if sys.platform != "win32" else "."
        checkpoint_path = "SAVING_SKIPPED"

        if self.arguments.track_memory:
            initialise_tracking(dir_prefix=dir_prefix)
            self.arguments._stop_capturing_memory = True
            mem_tracker = self._start_capturing_memory(dir_prefix=dir_prefix)
            if self.arguments.use_wandb:
                mem_tracker.start()

            else:
                warnings.warn(
                    "`track_memory` will be ignored since you are not using wandb"
                )
        start_time = time.time()
        sharded_state, shard_fns, gather_fns = self.initialize_state(
            model_parameters=model_parameters,
            state=state
        )

        count_model_parameters(sharded_state.params)
        with self.mesh:
            pbar = tqdm(total=self.max_training_steps)
            current_step = int(jax.device_get(sharded_state.step))
            loss_sum = None
            accuracy_sum = None
            pbar.update(sharded_state.step.tolist())
            if self.wandb_runtime is not None:
                model_parameters_number = sum(
                    n.size for n in
                    jax.tree_util.tree_flatten(flax.core.unfreeze(sharded_state.params))[0]
                ) / 1e9
                self.wandb_runtime.log(
                    {
                        "Number of Model Parameters (Billion)": model_parameters_number
                    }
                )
                wandb.summary["Number of Model Parameters (Billion)"] = model_parameters_number
            try:
                for epoch in range(self.arguments.num_train_epochs):
                    time_s = time.time()
                    for batch in self.dataloader_train:
                        current_step += 1
                        if (
                                self.arguments.step_start_point is not None
                                and
                                self.arguments.step_start_point > current_step
                        ):
                            pbar.update(1)
                        elif current_step < self.max_training_steps:

                            time_prev = time_s
                            time_s = time.time()
                            step_time = time_s - time_prev

                            batch["labels"] = (
                                batch["labels"][..., 1:]
                                if "labels" in batch and not self.arguments.train_on_inputs
                                else batch["input_ids"][..., 1:]
                            )
                            for ssb in self.arguments.ids_to_pop_from_dataset:
                                _ = batch.pop(ssb, None)

                            forward_backward_step_time_start = time.time()

                            (
                                sharded_state,
                                loss,
                                metrics,
                            ) = self.sharded_train_step_function(sharded_state, batch)

                            forward_backward_step_time_end = time.time()

                            pbar.update(1)

                            gathering_metrics_time_start = time.time()
                            with jax.spmd_mode("allow_all"):
                                loss_sum = loss if loss_sum is None else loss_sum + loss
                                accuracy = metrics["accuracy"]
                                accuracy_sum = accuracy if accuracy_sum is None else accuracy_sum + accuracy
                                train_metrics = {
                                    "loss": loss.tolist(),
                                    "mean_loss": (loss_sum / (current_step - self.arguments.step_start_point)).tolist(),
                                    "accuracy": accuracy,
                                    "mean_accuracy": (
                                            accuracy_sum / (current_step - self.arguments.step_start_point)
                                    ).tolist(),
                                    "learning_rate": self.scheduler(
                                        int(jax.device_get(sharded_state.step))
                                    ).tolist(),
                                    "step": int(jax.device_get(sharded_state.step)),
                                    "step_time": step_time,
                                    "perplexity": jnp.exp(loss).tolist(),
                                }
                                log_metrics = copy.deepcopy(train_metrics)
                                train_metrics.update(
                                    {
                                        "max_grad_norm": metrics["max_grad_norm"].tolist(),
                                        "mean_grad_norm": metrics["mean_grad_norm"].tolist(),
                                        "regularization_z_loss": metrics["regularization_z_loss"].tolist(),
                                        "epoch": epoch,
                                    }
                                )
                                train_metrics.update({
                                    f"grad_norm/{layer_name}": grad_norm.tolist()
                                    for layer_name, grad_norm in get_layer_names(metrics["grad_norms"]).items()
                                })

                            gathering_metrics_time_end = time.time()
                            pbar.set_postfix(**log_metrics)
                            train_metrics.update(
                                {
                                    "gathering_metrics_time": gathering_metrics_time_end - gathering_metrics_time_start,
                                    "forward_backward_step_time": (
                                            forward_backward_step_time_end - forward_backward_step_time_start
                                    )
                                }
                            )
                            if self.wandb_runtime is not None:
                                with jax.spmd_mode("allow_all"):
                                    self.wandb_runtime.log(train_metrics)
                            if self.arguments.training_time is not None:
                                if time.time() - start_time > self.arguments.training_time:
                                    raise EasyDelTimerError("Time Out")
                        else:
                            break
                        if self.arguments.save_steps is not None and current_step % self.arguments.save_steps == 0:
                            if self.rapture is None:
                                filename = self._save_state(
                                    state=sharded_state,
                                    gather_fns=gather_fns,
                                    milestone=True
                                )
                                checkpoint_path = f"{str(self.arguments.get_path())}/{filename}"
                            else:
                                print(
                                    termcolor.colored(
                                        "Info : ", color="red", force_color=True
                                    ),
                                    termcolor.colored(
                                        "You can not use `save_steps` while using LoRA "
                                        "right now. this action will be skipped", color="white", force_color=True
                                    )
                                )
            except KeyboardInterrupt:
                termcolor.cprint(
                    "KeyboardInterrupt At training model Will return Current State of the Model with Parameters.",
                    color="cyan",
                    force_color=True
                )

            except EasyDelTimerError:
                termcolor.cprint(
                    "Training reached out maximum training Time Killing training Process "
                    "and Will return Current State of the Model with Parameters.",
                    color="cyan",
                    force_color=True
                )
            if self.arguments.merge_lora_rapture_parameters and self.rapture is not None:
                print(
                    termcolor.colored(
                        "Info : ", color="red", force_color=True
                    ),
                    termcolor.colored(
                        "Merging LoRA Parameters.", color="white", force_color=True
                    )
                )
                sharded_state = sharded_state.replace(
                    params=self.rapture.merge_parameters(sharded_state.params)
                )
            output = TrainerOutput(
                state=sharded_state,
                mesh=self.mesh,
                shard_fns=shard_fns,
                gather_fns=gather_fns,
                checkpoint_manager=self.checkpoint_manager,
            )
            if self.arguments.save_steps is None or self.arguments.do_last_save:
                shard_fns, gather_fns = make_shard_and_gather_fns(
                    match_partition_rules(
                        self.config.get_partition_rules(
                            fully_sharded_data_parallel=self.arguments.fully_sharded_data_parallel
                        ) if self.arguments.custom_rule is None else self.arguments.custom_rule,
                        jax.eval_shape(lambda: sharded_state)
                    ),
                    dtype_specs=self.dtype
                )  # You have to re-init the new shard and gather functions in order to be able to skip LoRA weight
                # crashing errors and saving errors
                filename = self._save_state(
                    state=sharded_state,
                    gather_fns=gather_fns
                )
                checkpoint_path = f"{str(self.arguments.get_path())}/{filename}"

            if self.arguments.do_eval:
                for _ in self.eval(
                        sharded_state
                ):
                    ...

            output.checkpoint_path = checkpoint_path
            output.last_save_file_name = filename
            self.arguments._stop_capturing_memory = True
            wandb.finish()

            return output

    def eval(self, model_state: EasyDelState) -> typing.Iterator[dict]:
        """Evaluate the Given Model State and yield the eval metrics"""
        assert self.dataloader_eval is not None, "`dataloader_eval` is required by evaluator function."
        with self.mesh:

            dir_prefix: str = "/dev/shm" if sys.platform != "win32" else "."

            if self.arguments.track_memory:
                initialise_tracking(dir_prefix=dir_prefix)

            pbar = tqdm(total=self.max_evaluation_steps)
            pbar.set_description("Evaluating")
            current_step = 0
            loss_sum = None
            accuracy_sum = None

            try:
                for batch in self.dataloader_eval:
                    current_step += 1
                    time_start = time.time()
                    for key in self.arguments.ids_to_pop_from_dataset:
                        _ = batch.pop(key, None)
                    batch["labels"] = (
                        batch["labels"][..., 1:]
                        if "labels" in batch and not self.arguments.train_on_inputs
                        else batch["input_ids"][..., 1:]
                    )
                    metrics = self.sharded_eval_step_function(
                        model_state,
                        batch
                    )
                    total_time = time.time() - time_start
                    (
                        loss, accuracy
                    ) = metrics

                    loss_sum = loss.tolist() if loss_sum is None else loss_sum + loss
                    accuracy_sum = (
                        accuracy.tolist() if (
                                accuracy_sum is None
                        ) else accuracy_sum + accuracy
                    )

                    eval_metrics = {
                        "eval_loss": loss.tolist(),
                        "eval_mean_loss": loss_sum / (current_step - self.arguments.step_start_point),
                        "eval_mean_accuracy_sum": accuracy_sum / (
                                current_step - self.arguments.step_start_point
                        ),
                        "eval_step": current_step,
                        "eval_step_time": total_time,
                        "eval_perplexity": jnp.exp(loss).tolist(),
                    }
                    if self.arguments.use_wandb:
                        with jax.spmd_mode("allow_all"):
                            self.wandb_runtime.log(
                                eval_metrics
                            )

                    pbar.update(1)
                    log_metrics = copy.deepcopy(eval_metrics)
                    pbar.set_postfix(
                        **log_metrics
                    )
                    yield eval_metrics
            except KeyboardInterrupt:
                termcolor.cprint(
                    "KeyboardInterrupt At Evaluation model Will return Nothing and just pass.",
                    color="cyan",
                    force_color=True
                )
