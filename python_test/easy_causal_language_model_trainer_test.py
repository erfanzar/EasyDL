import os
import tensorflow_datasets as tfsd

os.environ["JAX_TRACEBACK_FILTERING"] = "off"
import flax.core

from lib.python.EasyDel import (
    CausalLanguageModelTrainer,
    AutoEasyDelModelForCausalLM,
    TrainArguments,
    FlaxLlamaForCausalLM,
    LlamaConfig
)
from jax import numpy as jnp, random
from datasets import Dataset

SEQUENCE_LENGTH = 128
DATA_ROW_SIZE = 1000
BATCH_SIZE = 32

MODEL_CONFIG = LlamaConfig(
    hidden_size=128,
    num_attention_heads=8,
    num_key_value_heads=4,
    num_hidden_layers=4,
    intermediate_size=256,
    gradient_checkpointing="",
    max_position_embeddings=SEQUENCE_LENGTH,
    use_scan_mlp=False,
)


def train():
    model = FlaxLlamaForCausalLM(
        config=MODEL_CONFIG,
        _do_init=True
    )
    params = model.params

    def data_generator():
        for i in range(DATA_ROW_SIZE):
            yield {
                "attention_mask": jnp.ones(
                    (1, SEQUENCE_LENGTH), dtype="i4"
                ),
                "input_ids": random.randint(
                    random.PRNGKey(0), (1, SEQUENCE_LENGTH), 0, 32000, dtype="i4"
                )
            }

    example_data = Dataset.from_generator(data_generator, )
    dtype = jnp.float32
    trainer = CausalLanguageModelTrainer(
        arguments=TrainArguments(
            model_name="CLM-Test",
            num_train_epochs=1,
            total_batch_size=1,
            gradient_accumulation_steps=1,
            use_wandb=False,
            model_class=type(model),
            do_shard_fns=False,
            max_sequence_length=SEQUENCE_LENGTH,
            configs_to_initialize_model_class={
                "config": model.config,
                "input_shape": (1, 1),
                "dtype": dtype,
                "param_dtype": dtype
            },
            dtype=dtype,
            param_dtype=dtype,
            track_memory=False,
            save_optimizer_state=True
        ),
        dataset_train=example_data,
    )
    output = trainer.train(model_parameters=flax.core.FrozenDict({"params": params}))
    return output.checkpoint_path


def re_train(checkpoint_path: str | os.PathLike):
    model = FlaxLlamaForCausalLM(
        config=MODEL_CONFIG,
        _do_init=False
    )

    def data_generator():
        for i in range(DATA_ROW_SIZE):
            yield {
                "attention_mask": jnp.ones(
                    (1, SEQUENCE_LENGTH), dtype="i4"
                ),
                "input_ids": random.randint(
                    random.PRNGKey(0), (1, SEQUENCE_LENGTH), 0, 32000, dtype="i4"
                )
            }

    example_data = Dataset.from_generator(data_generator, )
    dtype = jnp.float32
    trainer = CausalLanguageModelTrainer(
        arguments=TrainArguments(
            model_name="CLM-Test",
            num_train_epochs=4,
            total_batch_size=1,
            gradient_accumulation_steps=1,
            use_wandb=False,
            model_class=type(model),
            do_shard_fns=False,
            max_sequence_length=SEQUENCE_LENGTH,
            configs_to_initialize_model_class={
                "config": model.config,
                "input_shape": (1, 1),
                "dtype": dtype,
                "param_dtype": dtype
            },
            dtype=dtype,
            param_dtype=dtype,
            track_memory=False
        ),
        dataset_train=example_data,
        checkpoint_path=checkpoint_path
    )

    output = trainer.train()
    return output.checkpoint_path


if __name__ == "__main__":
    re_train(train())
