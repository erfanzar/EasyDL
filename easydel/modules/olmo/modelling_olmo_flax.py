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

import functools
import math
from typing import Optional, Tuple, Union

import chex
import jax
import jax.numpy as jnp
from flax import linen as nn
from flax.core import FrozenDict
from flax.core.frozen_dict import freeze, unfreeze
from flax.linen import Dense, combine_masks
from flax.linen.partitioning import remat
from flax.traverse_util import flatten_dict, unflatten_dict
from jax import lax
from jax.sharding import PartitionSpec

from easydel.layers.attention import FlaxAttentionModule, FlexibleAttentionModule
from easydel.layers.norms import LayerNormRaw

# easydel.modules
from easydel.modules.factory import register_module
from easydel.modules.flax_modeling_utils import (
	ACT2FN,
	apply_rotary_pos_emb,
	block_wise_ffn,
	control_mlp_sharding,
	get_dot_general_by_bits,
	get_gradient_checkpoint_policy,
	precompute_frequencies,
	with_sharding_constraint,
)
from easydel.modules.modeling_flax_outputs import (
	FlaxBaseModelOutput,
	FlaxCausalLMOutput,
)
from easydel.modules.modeling_utils import EasyDeLBaseModule
from easydel.modules.olmo.olmo_configuration import OlmoConfig

re_mat = remat


class FlaxOlmoRotaryEmbedding(nn.Module):
	dtype: jnp.dtype = jnp.float32

	def __call__(self, key, query, frequencies, position_ids):
		sin, cos = frequencies

		sin = sin[position_ids][:, None, :, :]
		cos = cos[position_ids][:, None, :, :]

		key = apply_rotary_pos_emb(key, sin, cos)
		query = apply_rotary_pos_emb(query, sin, cos)

		return query.astype(self.dtype), key.astype(self.dtype)


class FlaxOlmoMLP(nn.Module):
	config: OlmoConfig
	dtype: jnp.dtype = jnp.bfloat16
	param_dtype: jnp.dtype = jnp.bfloat16
	precision: Optional[Union[str, jax.lax.Precision]] = None

	def setup(self) -> None:
		dense = functools.partial(
			Dense,
			use_bias=False,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			precision=self.precision,
			kernel_init=nn.initializers.normal(),
			**get_dot_general_by_bits(self.config.bits, self.config.easy_method),
		)
		self.gate_proj = dense(self.config.intermediate_size)
		self.up_proj = dense(self.config.intermediate_size)
		self.down_proj = dense(self.config.hidden_size)
		self.act_fn = ACT2FN[self.config.hidden_act]

	def __call__(self, x: chex.Array, e: bool = False):  # Ignored
		x = control_mlp_sharding(x, self.config.partition_axis)

		return self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))


class FlaxOlmoAttention(FlaxAttentionModule):
	"""
	FlaxOlmoAttention implements an attention mechanism with rotary embeddings.

	Attributes:
	    config (OlmoConfig): Configuration for the attention module.
	    dtype (jnp.dtype): Data type for computations (default is jnp.bfloat16).
	    param_dtype (jnp.dtype): Data type for parameters (default is jnp.bfloat16).
	    precision (Optional[Union[str, jax.lax.Precision]]): Precision setting for JAX operations (default is "fastest").
	"""

	config: OlmoConfig
	dtype: jnp.dtype = jnp.float32
	param_dtype: jnp.dtype = jnp.float32
	precision: Optional[Union[jax.lax.Precision, str]] = None

	def setup(self):
		config = self.config
		self.hidden_size = config.hidden_size
		self.head_dim = self.config.hidden_size // self.config.num_attention_heads
		self.num_key_value_groups = (
			self.config.num_attention_heads // self.config.num_key_value_heads
		)

		if self.num_key_value_groups == 1:
			assert self.config.num_attention_heads == self.config.num_key_value_heads
		self.q_proj = Dense(
			config.num_attention_heads * self.head_dim,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			use_bias=self.config.attention_bias,
			kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
			precision=self.precision,
			**get_dot_general_by_bits(self.config.bits, self.config.easy_method),
		)
		self.k_proj = Dense(
			config.num_key_value_heads * self.head_dim,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			use_bias=self.config.attention_bias,
			kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
			precision=self.precision,
			**get_dot_general_by_bits(self.config.bits, self.config.easy_method),
		)
		self.v_proj = Dense(
			config.num_key_value_heads * self.head_dim,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			use_bias=self.config.attention_bias,
			kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
			precision=self.precision,
			**get_dot_general_by_bits(self.config.bits, self.config.easy_method),
		)
		self.o_proj = Dense(
			config.hidden_size,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			use_bias=False,
			kernel_init=jax.nn.initializers.normal(self.config.initializer_range),
			precision=self.precision,
			**get_dot_general_by_bits(self.config.bits, self.config.easy_method),
		)

		self.rotary = FlaxOlmoRotaryEmbedding(self.dtype)
		self.attention_performer = FlexibleAttentionModule(
			attention_dropout=self.config.attention_dropout,
			num_q_heads=self.config.num_attention_heads,
			num_kv_heads=self.config.num_key_value_heads,
			head_dims=self.head_dim,
			precision=self.precision,
			force_float32_tpu=True,
			attn_mechanism=self.config.attn_mechanism,
			dtype=self.config.attn_dtype,
			mesh=self.config.mesh,
			sm_scale=1 / math.sqrt(self.head_dim),
			axis_name=self.config.attention_axis_name,
			base_config=self.config,
		)

	def _merge_heads(self, hidden_states):
		"""
		Merges the attention heads into a single hidden state tensor.

		Args:
		    hidden_states (chex.Array): The hidden states with separate head dimensions.

		Returns:
		    chex.Array: The hidden states with merged head dimensions.
		"""
		return hidden_states.reshape(hidden_states.shape[:2] + (self.hidden_size,))

	def apply_rotary(self, query, key, frequencies, position_ids):
		"""
		Applies rotary positional embeddings to the query and key tensors.

		Args:
		    query (chex.Array): Query tensor.
		    key (chex.Array): Key tensor.
		    frequencies (Tuple[chex.Array, chex.Array]): Tuple containing cosine and sine components for rotary embeddings.
		    position_ids (chex.Array): Position indices for the tokens.

		Returns:
		    Tuple[chex.Array, chex.Array]: The modified query and key tensors after applying rotary embeddings.
		"""

		query, key = self._transpose_sequence_head(
			query,
			key,
		)
		query, key = self.rotary(
			position_ids=position_ids,
			query=query,
			key=key,
			frequencies=frequencies,
		)
		return self._transpose_sequence_head(query, key)

	def __call__(
		self,
		hidden_states: chex.Array,
		frequencies: Tuple[chex.Array, chex.Array],
		attention_mask: chex.Array,
		position_ids: chex.Array,
		causal_mask: chex.Array,
		segment_ids: Optional[chex.Array] = None,
		deterministic: bool = True,
		init_cache: bool = False,
		output_attentions: bool = False,
		fcm_mask: Optional[chex.Array] = None,
	):
		"""
		Forward pass of the attention module.

		Args:
		    hidden_states (chex.Array): Input hidden states.
		    frequencies (Tuple[chex.Array, chex.Array]): Cosine and sine components for rotary embeddings.
		    attention_mask (chex.Array): Mask to apply on the attention scores.
		    position_ids (chex.Array): Position indices for the tokens.
		    causal_mask (chex.Array): Causal mask for ensuring autoregressive behavior.
		    segment_ids (Optional[chex.Array]): Segment IDs for segment-based attention (optional).
		    deterministic (bool): If True, disables dropout for deterministic behavior.
		    init_cache (bool): If True, initializes cache for caching keys and values.
		    output_attentions (bool): If True, outputs attention weights alongside the hidden states.
		    fcm_mask (Optional[chex.Array]): fcm mask to be combined with attn mask and causal mask.
		Returns:
		    Tuple[chex.Array, chex.Array]: A tuple containing the attention output and the attention weights.
		"""
		batch_size, sequence_length = hidden_states.shape[:2]
		query_states, key_states, value_states = (
			self.q_proj(hidden_states),
			self.k_proj(hidden_states),
			self.v_proj(hidden_states),
		)

		if self.config.clip_qkv is not None:
			query_states, key_states, value_states = map(
				lambda x: jnp.clip(x, min=-self.config.clip_qkv, max=self.config.clip_qkv),
				[query_states, key_states, value_states],
			)

		query_states = query_states.reshape(
			batch_size,
			sequence_length,
			self.config.num_attention_heads,
			self.head_dim,
		)
		key_states = key_states.reshape(
			batch_size,
			sequence_length,
			self.config.num_key_value_heads,
			self.head_dim,
		)
		value_states = value_states.reshape(
			batch_size,
			sequence_length,
			self.config.num_key_value_heads,
			self.head_dim,
		)

		query_states, key_states = self.apply_rotary(
			query=query_states,
			key=key_states,
			position_ids=position_ids,
			frequencies=frequencies,
		)

		query_length, key_length = query_states.shape[1], key_states.shape[1]

		if self.has_variable("cache", "cached_key"):
			mask_shift = self.variables["cache"]["cache_index"]
			max_decoder_length = self.variables["cache"]["cached_key"].shape[1]
			causal_mask = lax.dynamic_slice(
				causal_mask,
				(0, 0, mask_shift, 0),
				(1, 1, query_length, max_decoder_length),
			)
		else:
			causal_mask = causal_mask[:, :, :query_length, :key_length]

		batch_size = hidden_states.shape[0]
		causal_mask = jnp.broadcast_to(causal_mask, (batch_size,) + causal_mask.shape[1:])
		if attention_mask.ndim == 2:
			attention_mask = jnp.expand_dims(attention_mask, axis=(-3, -2))
		attention_mask = jnp.broadcast_to(attention_mask, causal_mask.shape)
		attention_mask = combine_masks(attention_mask, causal_mask, fcm_mask)

		dropout_rng = None

		if not deterministic and self.config.attention_dropout > 0.0:
			dropout_rng = self.make_rng("dropout")
		if self.has_variable("cache", "cached_key") or init_cache:
			key_states, value_states, attention_mask = self._concatenate_to_cache(
				key_states,
				value_states,
				query_states,
				attention_mask,
			)

		attention_bias = lax.select(
			attention_mask > 0,
			jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
			jnp.full(attention_mask.shape, jnp.finfo(self.dtype).min).astype(self.dtype),
		)

		query_length, key_length = query_states.shape[1], key_states.shape[1]

		attentions = self.attention_performer(
			query_states=query_states,
			key_states=key_states,
			value_states=value_states,
			bias=attention_bias,
			attention_mask=attention_mask,
			causal=True,
			dropout_rng=dropout_rng,
			deterministic=deterministic,
			query_sequence_length=query_length,
			key_value_sequence_length=key_length,
			uses_cache=self.has_variable("cache", "cached_key") or init_cache,
			segment_ids=segment_ids,
			causal_mask=causal_mask,
		)

		attn_output = self._merge_heads(attentions.attention_outputs)
		if self.config.shard_attention_computation:
			attn_output = with_sharding_constraint(
				attn_output,
				PartitionSpec(
					self.config.partition_axis.batch_axis,
					(
						self.config.partition_axis.sequence_axis
						if attn_output.shape[1] != 1
						else None
					),
					self.config.partition_axis.hidden_state_axis,
				),
			)
		attn_output = self.o_proj(attn_output)

		outputs = (
			(attn_output, attentions.attention_weights)
			if output_attentions
			else (attn_output,)
		)
		return outputs


class FlaxOlmoDecoderLayer(nn.Module):
	config: OlmoConfig
	dtype: jnp.dtype = jnp.bfloat16
	param_dtype: jnp.dtype = jnp.bfloat16
	precision: Optional[jax.lax.Precision] = None

	def setup(self) -> None:
		attn_block = FlaxOlmoAttention
		mlp_block = FlaxOlmoMLP

		if self.config.gradient_checkpointing != "":
			attn_block = re_mat(
				attn_block,
				policy=get_gradient_checkpoint_policy(self.config.gradient_checkpointing),
				static_argnums=(1, 3, 4, 6, 7, 8),
			)
			mlp_block = re_mat(
				mlp_block,
				policy=get_gradient_checkpoint_policy(self.config.gradient_checkpointing),
				static_argnums=(1,),
			)
		self.self_attn = attn_block(
			config=self.config,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			precision=self.precision,
		)
		self.mlp = mlp_block(
			config=self.config,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			precision=self.precision,
		)
		self.input_layernorm = LayerNormRaw(
			eps=1e-5,
		)
		self.post_attention_layernorm = LayerNormRaw(
			eps=1e-5,
		)

	def __call__(
		self,
		hidden_states: chex.Array,
		frequencies: Tuple[chex.Array, chex.Array],
		attention_mask: chex.Array,
		position_ids: chex.Array,
		causal_mask: chex.Array,
		segment_ids: Optional[chex.Array] = None,
		deterministic: bool = True,
		init_cache: bool = False,
		output_attentions: bool = False,
		fcm_mask: Optional[chex.Array] = None,
	):
		"""
		Forward pass of the module block.

		Args:
		    hidden_states (chex.Array): Input hidden states.
		    frequencies (Tuple[chex.Array, chex.Array]): Cosine and sine components for rotary embeddings.
		    attention_mask (chex.Array): Mask to apply on the attention scores.
		    position_ids (chex.Array): Position indices for the tokens.
		    causal_mask (chex.Array): Causal mask for ensuring autoregressive behavior.
		    segment_ids (Optional[chex.Array]): Segment IDs for segment-based attention (optional).
		    deterministic (bool): If True, disables dropout for deterministic behavior.
		    init_cache (bool): If True, initializes cache for caching keys and values.
		    output_attentions (bool): If True, outputs attention weights alongside the hidden states.
		    fcm_mask (Optional[chex.Array]): fcm mask to be combined with attn mask and causal mask.
		Returns:
		    Tuple[chex.Array, chex.Array]: A tuple containing the attention output and the attention weights.
		"""

		residual = hidden_states
		attention_output = self.self_attn(
			self.input_layernorm(hidden_states),
			frequencies,
			attention_mask,
			position_ids,
			causal_mask,
			segment_ids,
			deterministic,
			init_cache,
			output_attentions,
			fcm_mask,
		)

		hidden_states = attention_output[0] + residual
		ffd_inp = self.post_attention_layernorm(hidden_states)
		if self.config.use_scan_mlp:
			feed_forward_hidden_states = block_wise_ffn(
				self.mlp,
				ffd_inp,
				self.config.scan_mlp_chunk_size,
				deterministic,
			)
		else:
			feed_forward_hidden_states = self.mlp(
				ffd_inp,
				deterministic,
			)

		hidden_states = hidden_states + feed_forward_hidden_states
		outputs = (hidden_states,)
		if output_attentions:
			outputs += (attention_output[1],)
		return outputs


class FlaxOlmoPretrainedModel(EasyDeLBaseModule):
	"""
	Base class for Olmo models providing initialization and configuration.

	Attributes:
	    config_class (OlmoConfig): The configuration class for the model.
	    module_class (nn.Module): The class representing the model's architecture.
	    base_model_prefix (str): The prefix for the base model parameters.
	"""

	config_class = OlmoConfig
	base_model_prefix = "model"
	module_class: nn.Module = None

	def __init__(
		self,
		config: OlmoConfig,
		dtype: jnp.dtype = jnp.bfloat16,
		param_dtype: jnp.dtype = jnp.bfloat16,
		precision: Optional[jax.lax.Precision] = None,
		input_shape: Tuple[int, int] = (1, 1),
		seed: int = 0,
		_do_init: bool = False,
		**kwargs,
	):
		"""
		Initializes the pre-trained model with the given configuration.

		Args:
		    config (OlmoConfig): Configuration for the model.
		    dtype (jnp.dtype): Data type for computations.
		    param_dtype (jnp.dtype): Data type for model parameters.
		    precision (Optional[jax.lax.Precision]): Precision setting for JAX operations.
		    input_shape (Tuple[int, int]): Shape of the input tensor.
		    seed (int): Seed for random number generation.
		    _do_init (bool): If True, initialize model weights.
		    **kwargs: Additional keyword arguments.
		"""
		module = self.module_class(
			config=config,
			dtype=dtype,
			param_dtype=param_dtype,
			precision=precision,
			**kwargs,
		)
		super().__init__(
			dtype=dtype,
			_do_init=_do_init,
			module=module,
			config=config,
			input_shape=input_shape,
			seed=seed,
		)

	def init_weights(
		self,
		rng: jax.random.PRNGKey,
		input_shape: Tuple,
		params: FrozenDict = None,
	) -> FrozenDict:
		"""
		Initializes the model weights.

		Args:
		    rng (jax.random.PRNGKey): Random number generator key.
		    input_shape (Tuple): Shape of the input tensor for initializing weights.
		    params (FrozenDict, optional): Existing parameters to initialize with.

		Returns:
		    FrozenDict: Initialized model parameters.
		"""
		input_ids = jnp.zeros(input_shape, dtype="i4")
		attention_mask = jnp.ones_like(input_ids)
		position_ids = jnp.broadcast_to(
			jnp.arange(jnp.atleast_2d(input_ids).shape[-1]), input_shape
		)
		params_rng, dropout_rng = jax.random.split(rng)
		rng_s = {"params": params_rng, "dropout": dropout_rng}

		if self.config.add_cross_attention:
			encoder_hidden_states = jnp.zeros(input_shape + (self.config.hidden_size,))
			encoder_attention_mask = attention_mask
			module_init_outputs = self.module.init(
				rng_s,
				input_ids,
				attention_mask,
				position_ids,
				encoder_hidden_states,
				encoder_attention_mask,
				return_dict=False,
			)
		else:
			module_init_outputs = self.module.init(
				rng_s,
				input_ids=input_ids,
				attention_mask=attention_mask,
				position_ids=position_ids,
				return_dict=False,
			)

		random_params = module_init_outputs["params"]

		if params is not None:
			random_params = flatten_dict(unfreeze(random_params))
			params = flatten_dict(unfreeze(params))
			for missing_key in self._missing_keys:
				params[missing_key] = random_params[missing_key]
			self._missing_keys = set()
			return freeze(unflatten_dict(params))
		else:
			return random_params

	def init_cache(self, batch_size, max_length):
		"""
		Initializes the cache for autoregressive generation.

		Args:
		    batch_size (int): Batch size for the cache.
		    max_length (int): Maximum length for the cache.

		Returns:
		    dict: Initialized cache.
		"""

		return super().init_cache(batch_size=batch_size, max_length=max_length)

	def __call__(
		self,
		input_ids: Optional[chex.Array] = None,
		input_embeds: Optional[chex.Array] = None,
		attention_mask: Optional[chex.Array] = None,
		position_ids: Optional[chex.Array] = None,
		segment_ids: Optional[chex.Array] = None,
		params: dict = None,
		past_key_values: Optional[dict] = None,
		dropout_rng: jax.random.PRNGKey = None,
		train: bool = False,
		output_attentions: Optional[bool] = None,
		output_hidden_states: Optional[bool] = None,
		return_dict: Optional[bool] = None,
		add_params_field: bool = False,
		**kwargs,
	):
		"""
		Forward pass through the model.

		Args:
		    input_ids (chex.Array): Input tensor containing token IDs.
		    input_embeds (Optional[chex.Array]): embedding inputs to be used instead of input_ids.
		    attention_mask (Optional[chex.Array]): Mask for attention.
		    position_ids (Optional[chex.Array]): Positional indices.
		    segment_ids (Optional[chex.Array]): Segment IDs for distinguishing different parts of the input.
		    params (dict, optional): Parameters for the model.
		    past_key_values (dict, optional): Past key and value states for caching.
		    dropout_rng (jax.random.PRNGKey, optional): RNG key for dropout.
		    train (bool): If True, the model is in training mode.
		    output_attentions (Optional[bool]): If True, output attention weights.
		    output_hidden_states (Optional[bool]): If True, output hidden states.
		    return_dict (Optional[bool]): If True, return a dictionary of outputs.
		    add_params_field (bool): If True, include the parameters in the input dictionary.
		    **kwargs: Additional arguments.

		Returns:
		    Output type depends on the model configuration.
		"""
		output_attentions = (
			output_attentions
			if output_attentions is not None
			else self.config.output_attentions
		)
		output_hidden_states = (
			output_hidden_states
			if output_hidden_states is not None
			else self.config.output_hidden_states
		)
		return_dict = return_dict if return_dict is not None else self.config.return_dict
		batch_size, sequence_length = (
			input_ids.shape if input_ids is not None else input_embeds.shape[:2]
		)

		if position_ids is None:
			if past_key_values is not None:
				raise ValueError(
					"Make sure to provide `position_ids` when passing `past_key_values`."
				)

			position_ids = jnp.broadcast_to(
				jnp.arange(sequence_length)[None, :], (batch_size, sequence_length)
			)

		if attention_mask is None:
			attention_mask = jnp.ones((batch_size, sequence_length))

		rng_s = {}
		if dropout_rng is not None:
			rng_s["dropout"] = dropout_rng

		inputs = (
			{"params": params or self.params} if add_params_field else params or self.params
		)

		if self.config.bits is not None:
			rng_s["params"] = jax.random.key(0)
		if past_key_values is not None:
			inputs["cache"] = past_key_values
			mutable = ["cache"]
		else:
			mutable = False

		outputs = self.module.apply(
			inputs,
			input_ids=jnp.array(input_ids, dtype="i4"),
			input_embeds=input_embeds,
			attention_mask=jnp.array(attention_mask, dtype="i4"),
			position_ids=jnp.array(position_ids, dtype="i4"),
			deterministic=not train,
			init_cache=False,
			output_attentions=output_attentions,
			output_hidden_states=output_hidden_states,
			return_dict=return_dict,
			segment_ids=segment_ids,
			rngs=rng_s,
			mutable=mutable,
		)

		if past_key_values is not None and return_dict:
			outputs, past_key_values = outputs
			outputs["past_key_values"] = unfreeze(past_key_values["cache"])
			return outputs
		elif past_key_values is not None and not return_dict:
			outputs, past_key_values = outputs
			outputs = outputs[:1] + (unfreeze(past_key_values["cache"]),) + outputs[1:]

		return outputs


class FlaxOlmoDecoratorCollection(nn.Module):
	"""
	FlaxOlmoDecoratorCollection represents a single layer in a Transformer-like model,
	incorporating self-attention and MLP.

	Attributes:
	    config (OlmoConfig): Configuration object containing model parameters.
	    dtype (jnp.dtype): Data type for computations (default is jnp.bfloat16).
	    param_dtype (jnp.dtype): Data type for model parameters (default is jnp.bfloat16).
	    precision (Optional[Union[str, jax.lax.Precision]]): Precision setting for JAX operations (default is "fastest").
	"""

	config: OlmoConfig
	dtype: jnp.dtype = jnp.bfloat16
	param_dtype: jnp.dtype = jnp.bfloat16
	precision: Optional[Union[str, jax.lax.Precision]] = None

	def setup(self) -> None:
		self.layers = [
			FlaxOlmoDecoderLayer(
				config=self.config,
				dtype=self.dtype,
				param_dtype=self.param_dtype,
				precision=self.precision,
				name=str(i),
			)
			for i in range(self.config.num_hidden_layers)
		]

	def __call__(
		self,
		hidden_states: chex.Array,
		frequencies: Tuple[chex.Array, chex.Array],
		attention_mask: chex.Array,
		causal_mask: chex.Array,
		position_ids: chex.Array,
		segment_ids: Optional[chex.Array] = None,
		deterministic: bool = True,
		init_cache: bool = False,
		output_attentions: bool = False,
		output_hidden_states: bool = False,
	) -> Tuple[chex.Array, Optional[chex.Array], chex.Array]:
		"""
		Forward pass through the collection of decoder layers.

		Args:
		    hidden_states (chex.Array): Input tensor containing the hidden states.
		    frequencies (Tuple[chex.Array, chex.Array]): Frequency positional encodings.
		    attention_mask (chex.Array): Mask to apply during attention.
		    causal_mask (chex.Array): Causal mask for autoregressive decoding.
		    position_ids (chex.Array): Positional indices for the sequence.
		    segment_ids (Optional[chex.Array]): Segment IDs for distinguishing different parts of the input.
		    deterministic (bool): If True, disables dropout.
		    init_cache (bool): If True, initializes caching mechanism for fast decoding.
		    output_attentions (bool): If True, returns attention weights.
		    output_hidden_states (bool): If True, returns hidden states.

		Returns:
		    Tuple[chex.Array, Optional[chex.Array], chex.Array]:
		        - hidden_states: The output tensor after layer processing.
		        - all_hidden_states: all of Hidden states (if `output_hidden_states` is True).
		        - self_attn_weights: Attention weights (if `output_attentions` is True).

		"""
		all_attentions = () if output_attentions else None
		all_hidden_states = () if output_hidden_states else None
		if not deterministic and self.config.fcm_max_ratio > 0:
			# Apply forgetful causal mask
			batch_size, seq_length = hidden_states.shape[0], hidden_states.shape[1]
			fcm_ratio = jax.random.uniform(
				self.make_rng("fcm"),
				shape=(batch_size, 1, 1, 1),
				minval=self.config.fcm_min_ratio,
				maxval=self.config.fcm_max_ratio,
			)
			fcm_mask = (
				jax.random.uniform(
					self.make_rng("fcm"), shape=(batch_size, 1, seq_length, seq_length)
				)
				> fcm_ratio
			)
			fcm_mask = fcm_mask.at[:, :, :, 0].set(True)
			fcm_mask = fcm_mask.astype("bool")
		else:
			fcm_mask = None
		for layer in self.layers:
			if output_hidden_states:
				all_hidden_states += (hidden_states,)

			output = layer(
				hidden_states=hidden_states,
				frequencies=frequencies,
				attention_mask=attention_mask,
				position_ids=position_ids,
				causal_mask=causal_mask,
				deterministic=deterministic,
				init_cache=init_cache,
				output_attentions=output_attentions,
				fcm_mask=fcm_mask,
				segment_ids=segment_ids,
			)
			hidden_states = output[0]

			if output_attentions:
				output_attentions += (output[1],)

		return hidden_states, all_hidden_states, all_attentions


class FlaxOlmoModule(nn.Module):
	"""
	Core module of the Olmo model, including embedding, decoder layers, and normalization.

	Attributes:
	    config (OlmoConfig): Configuration object with model hyperparameters.
	    dtype (jnp.dtype): Data type for the computations.
	    param_dtype (jnp.dtype): Data type for the model parameters.
	    precision (Optional[jax.lax.Precision]): Precision setting for JAX operations.
	"""

	config: OlmoConfig
	dtype: jnp.dtype = jnp.bfloat16
	param_dtype: jnp.dtype = jnp.bfloat16
	precision: Optional[Union[jax.lax.Precision, str]] = None

	def setup(self):
		self.embed_tokens = nn.Embed(
			self.config.vocab_size,
			self.config.hidden_size,
			embedding_init=jax.nn.initializers.normal(stddev=self.config.initializer_range),
			dtype=self.dtype,
			param_dtype=self.param_dtype,
		)

		self.layers = FlaxOlmoDecoratorCollection(
			self.config,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			precision=self.precision,
		)
		self.norm = LayerNormRaw(
			eps=1e-5,
		)

		initial_rope_kwargs = dict(rope_type="none")
		if self.config.rope_scaling is not None:
			scaling_type = self.config.rope_scaling["type"]
			scaling_factor = self.config.rope_scaling["factor"]
			initial_rope_kwargs = dict(
				scaling_factor=scaling_factor,
				rope_type=scaling_type,
			)

		self.frequencies = precompute_frequencies(
			max_position_embeddings=self.config.granted_freq_max_position_embedding,
			dim=self.config.hidden_size // self.config.num_attention_heads,
			base=self.config.rope_theta,
			**initial_rope_kwargs,
		)
		self.causal_mask = nn.make_causal_mask(
			jnp.ones(
				shape=(1, self.config.granted_mask_max_position_embedding),
				dtype="bool",
			),
			dtype="bool",
		)

	def __call__(
		self,
		input_ids: chex.Array,
		attention_mask: Optional[chex.Array] = None,
		position_ids: Optional[chex.Array] = None,
		segment_ids: Optional[chex.Array] = None,
		input_embeds: Optional[chex.Array] = None,
		output_attentions: Optional[bool] = None,
		output_hidden_states: Optional[bool] = None,
		init_cache: bool = False,
		deterministic: bool = True,
		return_dict: bool = True,
	) -> Union[FlaxBaseModelOutput, Tuple]:
		"""
		Forward pass through the Olmo module.

		Args:
		    input_ids (chex.Array): Input tensor containing token IDs.
		    attention_mask (chex.Array): Mask for attention.
		    position_ids (chex.Array): Positional indices.
		    segment_ids (Optional[chex.Array]): Segment IDs for different input parts.
		    input_embeds (Optional[chex.Array]): Embedded input tensor.
		    output_attentions (Optional[bool]): If True, output attention weights.
		    output_hidden_states (Optional[bool]): If True, output hidden states.
		    init_cache (bool): If True, initialize cache for decoding.
		    deterministic (bool): If True, disable dropout.
		    return_dict (bool): If True, return a dictionary of outputs.

		Returns:
		    FlaxBaseModelOutput | Tuple: Model output, either as a named tuple or a standard tuple.
		"""
		if input_embeds is None and input_ids is not None:
			input_embeds = self.embed_tokens(input_ids.astype("i4"))
		else:
			raise ValueError("you should specify input_embeds or input_ids one of them")
		batch_size, sequence_length, _ = input_embeds.shape

		assert (
			sequence_length <= self.config.max_position_embeddings
		), f"Maximum Position Embedding Reached ! (Excepted <= {self.config.max_position_embeddings} got {sequence_length})"
		if attention_mask.ndim == 2:
			attention_mask = jnp.expand_dims(attention_mask, (1, 2))

		outputs = self.layers(
			hidden_states=input_embeds,
			attention_mask=attention_mask,
			position_ids=position_ids,
			frequencies=self.frequencies,
			init_cache=init_cache,
			output_attentions=output_attentions,
			deterministic=deterministic,
			causal_mask=self.causal_mask,
			segment_ids=segment_ids,
			output_hidden_states=output_hidden_states,
		)

		hidden_states = outputs[0]
		hidden_states = self.norm(hidden_states)

		if output_hidden_states:
			all_hidden_states = outputs[1] + (hidden_states,)
			outputs = (hidden_states, all_hidden_states) + outputs[2:]
		else:
			outputs = (hidden_states,) + outputs[1:]

		if not return_dict:
			return tuple(value for value in outputs if value is not None)

		return FlaxBaseModelOutput(
			last_hidden_state=hidden_states,
			hidden_states=outputs[1],
			attentions=outputs[-1],
		)


@register_module(
	"base-module",
	config=OlmoConfig,
	model_type="olmo",
	embedding_layer_names=["embed_tokens"],
)
class FlaxOlmoModel(FlaxOlmoPretrainedModel):
	module_class = FlaxOlmoModule

	def set_input_embeddings(self, value):
		self.module.embed_tokens = value

	def get_input_embeddings(self):
		return self.module.embed_tokens


class FlaxOlmoForCausalLMModule(nn.Module):
	config: OlmoConfig
	dtype: jnp.dtype = jnp.bfloat16
	param_dtype: jnp.dtype = jnp.bfloat16
	precision: Optional[Union[jax.lax.Precision, str]] = None

	def setup(self):
		self.model: FlaxOlmoModule = FlaxOlmoModule(
			self.config,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			precision=self.precision,
		)

		self.lm_head = Dense(
			self.config.vocab_size,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			use_bias=False,
			kernel_init=jax.nn.initializers.normal(stddev=self.config.initializer_range),
			precision=self.precision,
			**get_dot_general_by_bits(self.config.bits, self.config.easy_method),
		)

	def __call__(
		self,
		input_ids: Optional[chex.Array] = None,
		attention_mask: Optional[chex.Array] = None,
		position_ids: Optional[chex.Array] = None,
		segment_ids: Optional[chex.Array] = None,
		input_embeds: Optional[chex.Array] = None,
		output_attentions: Optional[bool] = None,
		output_hidden_states: Optional[bool] = None,
		init_cache: bool = False,
		deterministic: bool = True,
		return_dict: bool = True,
	) -> Union[FlaxCausalLMOutput, Tuple]:
		"""
		Forward pass through the Olmo module.

		Args:
		    input_ids (Optional[chex.Array]): Input tensor containing token IDs.
		    attention_mask (Optional[chex.Array]): Mask for attention.
		    position_ids (Optional[chex.Array]): Positional indices.
		    segment_ids (Optional[chex.Array]): Segment IDs for different input parts.
		    input_embeds (Optional[chex.Array]): Embedded input tensor.
		    output_attentions (Optional[bool]): If True, output attention weights.
		    output_hidden_states (Optional[bool]): If True, output hidden states.
		    init_cache (bool): If True, initialize cache for decoding.
		    deterministic (bool): If True, disable dropout.
		    return_dict (bool): If True, return a dictionary of outputs.

		Returns:
		    FlaxCausalLMOutput | Tuple: Model output, either as a named tuple or a standard tuple.
		"""

		batch_size, seq_length = (
			input_ids.shape if input_ids is not None else input_embeds.shape[:2]
		)
		if attention_mask is None:
			attention_mask = jnp.ones_like(input_ids)
		if position_ids is None:
			position_ids = jnp.broadcast_to(
				jnp.clip(jnp.cumsum(attention_mask, axis=-1) - 1, a_min=0),
				(batch_size, seq_length),
			)
		outputs = self.model(
			input_ids=input_ids,
			attention_mask=attention_mask,
			position_ids=position_ids,
			deterministic=deterministic,
			input_embeds=input_embeds,
			init_cache=init_cache,
			output_attentions=output_attentions,
			output_hidden_states=output_hidden_states,
			return_dict=return_dict,
		)

		hidden_states = outputs[0]

		if self.config.tie_word_embeddings:
			shared_kernel = self.transformer.variables["params"]["embed_tokens"][
				"embedding"
			].T.astype(self.param_dtype)
			lm_logits = self.lm_head.apply(
				{"params": {"kernel": shared_kernel}},
				hidden_states,
			)
		else:
			lm_logits = self.lm_head(hidden_states)

		if not return_dict:
			return (lm_logits,) + outputs[1:]

		return FlaxCausalLMOutput(
			logits=lm_logits,
			hidden_states=outputs.hidden_states,
			attentions=outputs.attentions,
		)


@register_module(
	"causal-language-model",
	config=OlmoConfig,
	model_type="olmo",
	embedding_layer_names=["embed_tokens"],
)
class FlaxOlmoForCausalLM(FlaxOlmoPretrainedModel):
	module_class = FlaxOlmoForCausalLMModule

	def set_input_embeddings(self, value):
		self.module.model.embed_tokens = value

	def get_input_embeddings(self):
		return self.module.model.embed_tokens

	def set_decoder(self, decoder):
		self.module.model = decoder

	def get_decoder(self):
		return self.module.model

	def get_output_embeddings(self):
		return self.module.lm_head

	def set_output_embeddings(self, new_embeddings):
		self.module.lm_head = new_embeddings

	def prepare_inputs_for_generation(
		self, input_ids, max_length, attention_mask: Optional[chex.Array] = None
	):
		batch_size, seq_length = input_ids.shape

		past_key_values = self.init_cache(batch_size, max_length)
		extended_attention_mask = jnp.ones((batch_size, max_length), dtype="i4")
		if attention_mask is not None:
			position_ids = attention_mask.cumsum(axis=-1) - 1
			extended_attention_mask = jax.lax.dynamic_update_slice(
				extended_attention_mask, attention_mask, (0, 0)
			)
		else:
			position_ids = jnp.broadcast_to(
				jnp.arange(seq_length, dtype="i4")[None, :], (batch_size, seq_length)
			)

		return {
			"past_key_values": past_key_values,
			"attention_mask": extended_attention_mask,
			"position_ids": position_ids,
		}

	def update_inputs_for_generation(self, model_outputs, model_kwargs):
		model_kwargs["past_key_values"] = model_outputs.past_key_values
		model_kwargs["position_ids"] = model_kwargs["position_ids"][:, -1:] + 1
		return model_kwargs
