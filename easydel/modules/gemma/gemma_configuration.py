
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

from typing import Optional

from jax.sharding import PartitionSpec

from easydel.modules.factory import register_config
from easydel.modules.modeling_utils import EDPretrainedConfig

@register_config("gemma")
class GemmaConfig(EDPretrainedConfig):
	"""
	Configuration objects inherit from [`EDPretrainedConfig`] and can be used to control the model outputs. Read
	the documentation from [`EDPretrainedConfig`] for more information.

	Args:
	    vocab_size (`int`, *optional*, defaults to 256000):
	        Vocabulary size of the Gemma model. Defines the number of different tokens that can be represented by the
	        `inputs_ids` passed to the forward method.
	    hidden_size (`int`, *optional*, defaults to 3072):
	        Dimensionality of the encoder layers and the pooler layer.
	    intermediate_size (`int`, *optional*, defaults to 24576):
	        Dimensionality of the "intermediate" (i.e., feed-forward) layer in the Transformer encoder.
	    num_hidden_layers (`int`, *optional*, defaults to 28):
	        Number of hidden layers in the Transformer encoder.
	    num_attention_heads (`int`, *optional*, defaults to 16):
	        Number of attention heads for each attention layer in the Transformer encoder.
	    num_key_value_heads (`int`, *optional*, defaults to 16):
	        Number of key and value heads for each attention layer in the Transformer encoder.
	    head_dim (`int`, *optional*, defaults to 256):
	        Dimensionality of the attention head.
	    hidden_act (`str` or `function`, *optional*, defaults to `"gelu_pytorch_tanh"`):
	        The non-linear activation function (function or string) to use in the encoder and pooler. If string,
	        `"gelu"`, `"relu"`, `"swish"` and `"gelu_new"` are supported.
	    max_position_embeddings (`int`, *optional*, defaults to 8192):
	        The maximum sequence length that this model might ever be used with. Typically set this to something large
	        just in case (e.g., 2048 or 4096).
	    initializer_range (`float`, *optional*, defaults to 0.02):
	        The standard deviation of the truncated_normal_initializer for initializing all weight matrices.
	    rms_norm_eps (`float`, *optional*, defaults to 1e-6):
	        The epsilon used by the rms normalization layers.
	    use_cache (`bool`, *optional*, defaults to `True`):
	        Whether or not the model should return the last key/values attentions (not used by all models). Only
	        relevant if `config.is_decoder=True`.
	    pad_token_id (`int`, *optional*, defaults to 0):
	        The index of the padding token in the vocabulary.
	    eos_token_id (`int`, *optional*, defaults to 1):
	        The index of the end of sequence token in the vocabulary.
	    bos_token_id (`int`, *optional*, defaults to 2):
	        The index of the beginning of sequence token in the vocabulary.
	    tie_word_embeddings (`bool`, *optional*, defaults to `True`):
	        Whether to tie the weights of the input embeddings and the output embeddings.
	    rope_theta (`float`, *optional*, defaults to 10000.0):
	        The theta value to use for rotary position embeddings.
	    attention_bias (`bool`, *optional*, defaults to `False`):
	        Whether to use attention bias.
	    attention_dropout (`float`, *optional*, defaults to 0.0):
	        The dropout ratio for the attention probabilities.
	    gradient_checkpointing (`str`, *optional*, defaults to `"nothing_saveable"`):
	        The gradient checkpointing configuration.
	    bits (`int`, *optional*):
	        The number of bits to quantize the model to.
	    scan_layers (`bool`, *optional*, defaults to `False`):
	        Whether to use the scan implementation of the layers.
	    hidden_activation (`str`, *optional*):
	        The hidden activation function to use.
	"""

	model_type: str = "gemma"

	def __init__(
		self,
		vocab_size=256000,
		hidden_size=3072,
		intermediate_size=24576,
		num_hidden_layers=28,
		num_attention_heads=16,
		num_key_value_heads=16,
		head_dim=256,
		hidden_act="gelu_pytorch_tanh",
		max_position_embeddings=8192,
		initializer_range=0.02,
		rms_norm_eps=1e-6,
		use_cache=True,
		pad_token_id=0,
		eos_token_id=1,
		bos_token_id=2,
		tie_word_embeddings=True,
		rope_theta=10000.0,
		attention_bias=False,
		attention_dropout=0.0,
		gradient_checkpointing: str = "nothing_saveable",
		bits: Optional[int] = None,
		scan_layers: bool = False,
		hidden_activation="gelu_pytorch_tanh",
		**kwargs,
	):
		self.gradient_checkpointing = gradient_checkpointing
		self.bits = bits
		self.scan_layers = scan_layers
		self.vocab_size = vocab_size
		self.max_position_embeddings = max_position_embeddings
		self.hidden_size = hidden_size
		self.intermediate_size = intermediate_size
		self.num_hidden_layers = num_hidden_layers
		self.num_attention_heads = num_attention_heads
		self.head_dim = head_dim
		self.num_key_value_heads = num_key_value_heads
		self.hidden_act = hidden_act
		self.initializer_range = initializer_range
		self.rms_norm_eps = rms_norm_eps
		self.use_cache = use_cache
		self.rope_theta = rope_theta
		self.attention_bias = attention_bias
		self.attention_dropout = attention_dropout
		self.hidden_activation = hidden_activation
		super().__init__(
			bos_token_id=bos_token_id,
			eos_token_id=eos_token_id,
			pad_token_id=pad_token_id,
			tie_word_embeddings=tie_word_embeddings,
			bits=bits,
			**kwargs,
		)

	def get_partition_rules(self, fully_sharded_data_parallel: bool = True):
		"""
		Get the partition rules for the model.

		Args:
		    fully_sharded_data_parallel (`bool`, *optional*, defaults to `True`):
		        Whether to use fully sharded data parallelism.

		Returns:
		    `Tuple[Tuple[str, PartitionSpec]]`: The partition rules.
		"""
		return (
			(
				("model/embed_tokens/embedding", PartitionSpec("tp", ("fsdp", "sp"))),
				(
					"self_attn/(q_proj|k_proj|v_proj)/kernel",
					PartitionSpec(("fsdp", "sp"), "tp"),
				),
				("self_attn/o_proj/kernel", PartitionSpec("tp", ("fsdp", "sp"))),
				("mlp/gate_proj/kernel", PartitionSpec(("fsdp", "sp"), "tp")),
				("mlp/down_proj/kernel", PartitionSpec("tp", ("fsdp", "sp"))),
				("mlp/up_proj/kernel", PartitionSpec(("fsdp", "sp"), "tp")),
				("input_layernorm/kernel", PartitionSpec(None)),
				("post_attention_layernorm/kernel", PartitionSpec(None)),
				("model/norm/kernel", PartitionSpec(None)),
				("lm_head/kernel", PartitionSpec(("fsdp", "sp"), "tp")),
				(".*", PartitionSpec(None)),
			)
			if not fully_sharded_data_parallel
			else (
				("model/embed_tokens/embedding", PartitionSpec(("fsdp", "sp"))),
				(
					"self_attn/(q_proj|k_proj|v_proj)/kernel",
					PartitionSpec(("fsdp", "sp")),
				),
				("self_attn/o_proj/kernel", PartitionSpec(("fsdp", "sp"))),
				("mlp/gate_proj/kernel", PartitionSpec(("fsdp", "sp"))),
				("mlp/down_proj/kernel", PartitionSpec(("fsdp", "sp"))),
				("mlp/up_proj/kernel", PartitionSpec(("fsdp", "sp"))),
				("input_layernorm/kernel", PartitionSpec(None)),
				("post_attention_layernorm/kernel", PartitionSpec(None)),
				("model/norm/kernel", PartitionSpec(None)),
				("lm_head/kernel", PartitionSpec(("fsdp", "sp"))),
				(".*", PartitionSpec(("fsdp", "sp"))),
			)
		)

	def add_jax_args(
		self,
		gradient_checkpointing: str = "nothing_saveable",
		bits: Optional[int] = None,
		**kwargs,
	):
		"""The add_jax_args function adds the following arguments to the Transformer class:

		Args:
		    self: Refer to the current object
		    gradient_checkpointing: str: Control the amount of memory
		        used by jax
		    bits: Optional[int]: Determine the number of bits used in
		        the quantization
		"""
		self.gradient_checkpointing = gradient_checkpointing
		self.bits = bits

	@staticmethod
	def get_weight_decay_exclusions():
		return tuple()

	@staticmethod
	def rng_keys():
		return "params", "dropout", "fcm"

	@property
	def granted_freq_max_position_embedding(self) -> int:
		return getattr(
			self,
			"freq_max_position_embeddings",
			self.max_position_embeddings,
		)

	@property
	def granted_mask_max_position_embedding(self) -> int:
		return getattr(
			self,
			"mask_max_position_embeddings",
			self.max_position_embeddings,
		)