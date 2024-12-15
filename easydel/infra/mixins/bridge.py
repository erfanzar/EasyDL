# Copyright 2023 The EASYDEL Author @erfanzar (Erfan Zare Chavoshi).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import os
import typing as tp
from copy import deepcopy
from pathlib import Path

import jax
import jax.extend
import jax.tree_util
from flax import nnx as nn
from jax import numpy as jnp
from jax.sharding import PartitionSpec
from transformers.utils.generic import working_or_temp_dir
from transformers.utils.hub import PushToHubMixin

from easydel.etils.etils import (
	EasyDeLBackends,
	EasyDeLPlatforms,
	get_logger,
)
from easydel.etils.partition_module import PartitionAxis
from easydel.infra.base_config import EasyDeLBaseConfig
from easydel.utils.checkpoint_managers import CheckpointManager
from easydel.utils.readme_generator import ModelInfo, ReadmeGenerator
from easydel.utils.traversals import (
	flatten_dict,
	merge_model_and_tree,
	string_key_to_int,
	unflatten_dict,
)

logger = get_logger(__name__)

FLAX_WEIGHTS_NAME = "easydel-model.parameters"


class EasyBridgeMixin(PushToHubMixin):
	"""
	Mixin class for adding bridging functionalities like saving, loading, and pushing models to Hugging Face Hub.
	"""

	config_class: tp.Type[EasyDeLBaseConfig]
	config: EasyDeLBaseConfig
	base_model_prefix: str
	_model_task: tp.Optional[str] = None
	_model_type: tp.Optional[str] = None

	_model_type: str = None
	_model_task: str = None

	def _model_card(self, name: str, repo_id: str) -> str:
		"""Generates a model card (README.md) for the given model.

		Args:
		    name (str): The name of the model.
		    repo_id (str): The repository ID on Hugging Face Hub.

		Returns:
		    str: The generated README.md content.
		"""
		from easydel import __version__

		model_info = ModelInfo(
			name=name,
			type=self.__class__.__name__,
			repo_id=repo_id,
			model_type=self._model_type,
			model_task=self._model_task,
			version=__version__,
		)
		return ReadmeGenerator().generate_readme(model_info)

	def _save_model_files(
		self,
		save_directory: Path,
		gather_fns: tp.Optional[dict[tp.Callable]] = None,
		float_dtype=None,
		verbose: bool = True,
		mismatch_allowed: bool = True,
	):
		"""Saves the model's configuration, weights, and potentially the generation config to the specified directory.

		Args:
		  save_directory (Path): The directory where the model files will be saved.
		  gather_fns (dict[Callable], optional): Custom gather functions for checkpoint saving.
		  float_dtype (dtype, optional): Data type for saving weights. Defaults to None.
		  verbose (bool, optional): Whether to print verbose messages. Defaults to True.
		  mismatch_allowed (bool, optional): If True allows mismatch in parameters. Defaults to True.
		"""
		save_directory.mkdir(parents=True, exist_ok=True)

		config_to_save = deepcopy(self.config)
		config_to_save.__dict__.pop("attn_dtype", None)  # Make sure dtypes are not included
		config_to_save.architectures = [self.__class__.__name__]
		config_to_save.save_pretrained(str(save_directory))

		if self.can_generate():
			self.generation_config.save_pretrained(str(save_directory))

		output_model_file = save_directory / FLAX_WEIGHTS_NAME
		state = nn.split(self)[1]

		CheckpointManager.save_checkpoint(
			state=state.to_pure_dict(),
			path=str(output_model_file),
			gather_fns=gather_fns,
			mismatch_allowed=mismatch_allowed,
			float_dtype=float_dtype,
			verbose=verbose,
		)

		logger.info(f"Model weights saved in {output_model_file}")

	def save_pretrained(
		self,
		save_directory: tp.Union[str, os.PathLike],
		push_to_hub: bool = False,
		token: tp.Optional[tp.Union[str, bool]] = None,
		gather_fns: tp.Optional[dict[tp.Callable]] = None,
		float_dtype=None,
		verbose: bool = True,
		mismatch_allowed: bool = True,
		**kwargs,
	):
		"""Saves the model, its configuration, and optionally pushes it to the Hugging Face Hub.

		Args:
		    save_directory (str or PathLike): The directory where to save the model.
		    push_to_hub (bool, optional): If True, pushes the model to the Hugging Face Hub.
		    token (str or bool, optional): The Hugging Face Hub token.
		    gather_fns (dict[Callable], optional): Custom gather functions for checkpoint saving.
		    float_dtype (dtype, optional): Data type for saving weights.
		    verbose (bool, optional): Whether to print verbose messages. Defaults to True.
		    mismatch_allowed (bool, optional): If True, allows mismatch in parameters while loading. Defaults to True.
		    **kwargs: Additional keyword arguments for Hugging Face Hub.
		"""
		save_directory = Path(save_directory)

		if save_directory.is_file():
			logger.error(
				f"Provided path ({save_directory}) should be a directory, not a file"
			)
			return

		repo_id = kwargs.pop("repo_id", save_directory.name)
		if push_to_hub:
			commit_message = kwargs.pop("commit_message", None)
			repo_id = self._create_repo(repo_id, **kwargs)
			files_timestamps = self._get_files_timestamps(save_directory)

		self._save_model_files(
			save_directory, gather_fns, float_dtype, verbose, mismatch_allowed
		)

		readme_path = save_directory / "README.md"
		if not readme_path.exists():
			readme_path.write_text(self._model_card(repo_id, repo_id))

		if push_to_hub:
			self._upload_modified_files(
				str(save_directory),
				repo_id,
				files_timestamps,
				commit_message=commit_message,
				token=token,
			)

	def push_to_hub(
		self,
		repo_id: str,
		params: tp.Any,
		use_temp_dir: tp.Optional[bool] = None,
		commit_message: tp.Optional[str] = None,
		private: tp.Optional[bool] = None,
		token: tp.Optional[tp.Union[bool, str]] = None,
		create_pr: bool = False,
		safe_serialization: bool = True,
		gather_fns: tp.Optional[dict[tp.Callable]] = None,
		float_dtype=None,
		verbose: bool = True,
		mismatch_allowed: bool = True,
		revision: str = None,
		commit_description: str = None,
	) -> str:
		"""Pushes the model to the Hugging Face Hub.

		Args:
		    repo_id (str): The repository ID on Hugging Face Hub.
		    params (any): Model parameters.
		    use_temp_dir (bool, optional): If True, uses a temporary directory. Defaults to None
		    commit_message (str, optional): The commit message for the push.
		    private (bool, optional): If True, creates a private repository.
		    token (str or bool, optional): The Hugging Face Hub token.
		    create_pr (bool, optional): If True, creates a pull request.
		    safe_serialization (bool, optional): If True, uses safe serialization.
		    gather_fns (dict[Callable], optional): Custom gather functions for checkpoint saving.
		    float_dtype (dtype, optional): Data type for saving weights.
		    verbose (bool, optional): Whether to print verbose messages. Defaults to True.
		    mismatch_allowed (bool, optional): If True, allows mismatch in parameters while loading. Defaults to True.
		    revision (str, optional): The revision to push to.
		    commit_description (str, optional): The commit description for the push.

		Returns:
		    str: The URL of the created repository.
		"""
		working_dir = Path(repo_id.split("/")[-1])

		repo_id = self._create_repo(
			repo_id,
			private=private,
			token=token,
			repo_url=None,
			organization=None,
		)

		if use_temp_dir is None:
			use_temp_dir = not working_dir.is_dir()

		with working_or_temp_dir(
			working_dir=str(working_dir), use_temp_dir=use_temp_dir
		) as work_dir:
			work_dir_path = Path(work_dir)
			files_timestamps = self._get_files_timestamps(work_dir_path)
			self.save_pretrained(
				work_dir,
				push_to_hub=False,
				token=token,
				gather_fns=gather_fns,
				float_dtype=float_dtype,
				verbose=verbose,
				mismatch_allowed=mismatch_allowed,
				repo_id=repo_id,
				params=params,
			)

			return self._upload_modified_files(
				str(work_dir_path),
				repo_id,
				files_timestamps,
				commit_message=commit_message,
				token=token,
				create_pr=create_pr,
				revision=revision,
				commit_description=commit_description,
			)

	@classmethod
	def can_generate(cls) -> bool:
		"""Checks if the model can generate sequences with `.generate()`.

		Returns:
		    bool: True if the model can generate, False otherwise.
		"""
		# Detects whether `prepare_inputs_for_generation` has been overwritten, which is a requirement for generation.
		# Alternatively, the model can also have a custom `generate` function.
		# if "GenerationMixin" in str(
		# 	cls.prepare_inputs_for_generation
		# ) and "GenerationMixin" in str(cls.generate):
		# 	return False
		return True

	@classmethod
	def _load_model_weights(
		cls,
		resolved_archive_file: tp.Optional[str],
		model: nn.Module,
		mismatch_allowed: bool,
		verbose: bool,
		shard_fns: tp.Optional[dict[tp.Callable]],
	) -> nn.Module:
		"""Loads model weights from a checkpoint file.

		Args:
		    resolved_archive_file: The path to the checkpoint file.
		    model: A Flax model.
		    mismatch_allowed: If True, allows mismatch in parameters while loading.
		    verbose: Whether to print verbose messages.
		    shard_fns: Custom shard functions for loading checkpoint.

		Returns:
		    A flax Module, with loaded parameter.
		"""
		if resolved_archive_file:
			state, _ = CheckpointManager.load_checkpoint(
				path=resolved_archive_file,
				mismatch_allowed=mismatch_allowed,
				verbose=verbose,
				shard_fns=shard_fns,
				callback=None,
			)

			params = state.get("params", None)
			if params is not None:
				state = params
			state = flatten_dict(state)
			state = string_key_to_int(state)

			required_params = set(flatten_dict(model.graphtree_params_shape))
			unexpected_keys = set(state.keys()) - required_params

			for unexpected_key in unexpected_keys:
				del state[unexpected_key]

			return merge_model_and_tree(model=model, tree=unflatten_dict(state))

		else:
			return model

	@classmethod
	def from_pretrained(
		cls,
		pretrained_model_name_or_path: tp.Optional[tp.Union[str, os.PathLike]],
		sharding_axis_dims: tp.Sequence[int] = (1, -1, 1, 1),
		sharding_axis_names: tp.Sequence[str] = ("dp", "fsdp", "tp", "sp"),
		partition_axis: PartitionAxis = PartitionAxis(),  # noqa
		dtype: jnp.dtype = jnp.float32,
		param_dtype: jnp.dtype = jnp.float32,
		precision: jax.lax.PrecisionLike = jax.lax.Precision("fastest"),  # noqa
		config_kwargs: tp.Optional[dict[str, tp.Any]] = None,
		partition_rules: tp.Optional[tp.Tuple[tp.Tuple[str, PartitionSpec]]] = None,
		backend: tp.Optional[EasyDeLBackends] = None,
		platform: tp.Optional[EasyDeLPlatforms] = "jax",
		model_task: tp.Optional[str] = None,
		shard_fns: tp.Optional[dict[tp.Callable]] = None,
		auto_shard_model: bool = False,
		verbose: bool = True,
		mismatch_allowed: bool = True,
		*model_args,
		config: tp.Optional[tp.Union[EasyDeLBaseConfig, str, os.PathLike]] = None,
		cache_dir: tp.Optional[tp.Union[str, os.PathLike]] = None,
		force_download: bool = False,
		local_files_only: bool = False,
		token: tp.Optional[tp.Union[str, bool]] = None,
		revision: str = "main",
		**kwargs,
	):
		"""Loads an EasyDeL model from a pretrained model or path.

		Args:
		    pretrained_model_name_or_path (str, optional): The name or path of the pretrained model.
		    sharding_axis_dims (Sequence[int], optional): The dimensions of sharding axes.
		    sharding_axis_names (Sequence[str], optional): The names of sharding axes.
		    partition_axis (PartitionAxis, optional): The partition axis configuration.
		    dtype (dtype, optional): The data type of the model.
		    param_dtype (dtype, optional): The data type of the parameters.
		    precision (PrecisionLike, optional): The computation precision.
		    config_kwargs (dict[str, Any], optional): Additional configuration parameters.
		    partition_rules (tuple, optional): Custom partitioning rules for sharding.
		    backend (EasyDeLBackends, optional): The backend to use.
		    platform (EasyDeLPlatforms, optional): The platform to use.
		    model_task (str, optional): The model task.
		    shard_fns (dict[Callable], optional): Custom shard functions for loading checkpoint.
		    auto_shard_model (bool, optional): Whether to automatically shard the model.
		    verbose (bool, optional): Whether to print verbose messages. Defaults to True.
		    mismatch_allowed (bool, optional): If True, allows mismatch in parameters while loading. Defaults to True.
		    *model_args: Additional arguments for the model.
		    config (str, optional): configuration for the model.
		    cache_dir (str, optional): The cache directory for the pretrained model.
		    force_download (bool, optional): Whether to force download the model.
		    local_files_only (bool, optional): Whether to use only local files.
		    token (str, optional): The Hugging Face Hub token.
		    revision (str, optional): The revision of the model to load.
		    **kwargs: Additional keyword arguments.

		Returns:
		    The loaded EasyDeL model.
		"""

		from huggingface_hub import HfApi
		from transformers import GenerationConfig
		from transformers.utils import download_url as _download_url
		from transformers.utils import is_offline_mode as _is_offline_mode
		from transformers.utils import is_remote_url as _is_remote_url

		from easydel.infra.utils import quantize_linear_layers
		from easydel.modules.auto_configuration import (
			AutoEasyDeLConfig,
			AutoShardAndGatherFunctions,
			get_modules_by_type,
		)

		api = HfApi(token=token)

		proxies = kwargs.pop("proxies", None)
		trust_remote_code = kwargs.pop("trust_remote_code", None)
		from_pipeline = kwargs.pop("_from_pipeline", None)
		from_auto_class = kwargs.pop("_from_auto", False)
		subfolder = kwargs.pop("subfolder", "")
		commit_hash = kwargs.pop("_commit_hash", None)

		# Not relevant for Flax Models
		_ = kwargs.pop("adapter_kwargs", None)

		if trust_remote_code is True:
			logger.warning(
				"The argument `trust_remote_code` is to be used with Auto classes. It has no effect here and is"
				" ignored."
			)

		if _is_offline_mode() and not local_files_only:
			logger.info("Offline mode: forcing local_files_only=True")
			local_files_only = True

		config_path = config if config is not None else pretrained_model_name_or_path

		config = AutoEasyDeLConfig.from_pretrained(
			config_path,
			sharding_axis_dims=sharding_axis_dims,
			sharding_axis_names=sharding_axis_names,
			partition_axis=partition_axis,
			from_torch=False,
			backend=backend,
			platform=platform,
		)

		if config_kwargs:
			for k, v in config_kwargs.items():
				setattr(config, k, v)

		if commit_hash is None:
			commit_hash = getattr(config, "_commit_hash", None)
		if auto_shard_model and shard_fns is None:
			shard_fns, _ = AutoShardAndGatherFunctions.from_config(
				config=config,
				flatten=False,
				partition_rules=partition_rules,
			)
			fns = {"params": shard_fns}
			fns.update(shard_fns)
			shard_fns = fns

		elif auto_shard_model and shard_fns is not None:
			logger.warning(
				"`auto_shard_model` will be ignored since `shard_fns` is provided."
			)

		resolved_archive_file = None
		if pretrained_model_name_or_path:
			pretrained_model_name_or_path = str(pretrained_model_name_or_path)

			is_local = Path(pretrained_model_name_or_path).is_dir()

			if is_local:
				archive_file = (
					Path(pretrained_model_name_or_path) / subfolder / FLAX_WEIGHTS_NAME
				)
				if not archive_file.is_file():
					raise FileNotFoundError(
						f"No file named '{FLAX_WEIGHTS_NAME}' found in directory '{pretrained_model_name_or_path}'."
					)
			elif Path(subfolder) / pretrained_model_name_or_path:
				archive_file = Path(subfolder) / pretrained_model_name_or_path
				is_local = True
			elif _is_remote_url(pretrained_model_name_or_path):
				filename = pretrained_model_name_or_path
				resolved_archive_file = _download_url(pretrained_model_name_or_path)
			else:
				filename = FLAX_WEIGHTS_NAME
				try:
					resolved_archive_file = api.hf_hub_download(
						repo_id=pretrained_model_name_or_path,
						filename=filename,
						subfolder=subfolder,
						revision=revision,
						cache_dir=cache_dir,
						force_download=force_download,
						proxies=proxies,
						token=token,
						local_files_only=local_files_only,
					)

					if resolved_archive_file is None:
						raise FileNotFoundError("No model parameters found!")
				except FileNotFoundError:
					raise
				except Exception:
					raise EnvironmentError(
						f"Can't load the model for '{pretrained_model_name_or_path}'. If you were trying to load it"
						" from 'https://huggingface.co/models', make sure you don't have a local directory with the"
						f" same name. Otherwise, make sure '{pretrained_model_name_or_path}' is the correct path to a"
						f" directory containing a file named {FLAX_WEIGHTS_NAME}."
					) from None
			if is_local:
				logger.debug(f"loading weights file {archive_file}")
				resolved_archive_file = str(archive_file)
			else:
				logger.debug(
					f"loading weights file {filename} from cache at {resolved_archive_file}"
				)
		if model_task is None:
			model_task = "base-module"
		if cls.__name__ == "EasyDeLBaseModule":
			# if they are using EasyDeLBaseModule.from_pretrained
			# they will get error AssertionError: `module` must be provided.` so we autoset this to make sure user don't
			# experience this error.
			_, cls, _ = get_modules_by_type(config.model_type, model_task)

		model = nn.eval_shape(
			lambda: cls(
				config=config,
				dtype=dtype,
				param_dtype=param_dtype,
				precision=precision,
				rngs=nn.Rngs(0),
			)
		)
		model = quantize_linear_layers(
			model,
			method=config.quantization_method,
			block_size=config.quantization_blocksize,
			quantization_pattern=config.quantization_pattern,
		)

		model = cls._load_model_weights(
			resolved_archive_file,
			model,
			mismatch_allowed,
			verbose,
			shard_fns,
		)

		if model.can_generate():
			try:
				model.generation_config = GenerationConfig.from_pretrained(
					pretrained_model_name_or_path,
					cache_dir=cache_dir,
					force_download=force_download,
					proxies=proxies,
					local_files_only=local_files_only,
					token=token,
					revision=revision,
					subfolder=subfolder,
					_from_auto=from_auto_class,
					_from_pipeline=from_pipeline,
					**kwargs,
				)
			except OSError:
				logger.info(
					"Generation config file not found, using a generation config created from the model config."
				)

		return model