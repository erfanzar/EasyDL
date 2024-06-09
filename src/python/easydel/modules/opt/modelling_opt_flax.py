# coding=utf-8
# Copyright 2022 The Fairseq Authors and The Google Flax Team Authors And The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# THIS SCRIPT IS EDITED FROM ORIGINAL IMPLEMENTATION OF TRANSFORMERS OPT
"""Flax OPT model."""

from functools import partial

import fjformer
import flax.linen
from typing import Optional, Tuple

from fjformer import linen as nn
import jax
import jax.numpy as jnp
from flax.core.frozen_dict import FrozenDict, freeze, unfreeze
from flax.linen import combine_masks
from flax.linen.attention import dot_product_attention_weights
from flax.traverse_util import flatten_dict, unflatten_dict
from jax import lax
from jax.random import PRNGKey
from transformers.modeling_flax_outputs import FlaxBaseModelOutput, FlaxMaskedLMOutput
from transformers.modeling_flax_utils import ACT2FN
from jax.sharding import PartitionSpec
from transformers import logging

from ..flax_modelling_utils import get_gradient_checkpoint_policy, \
    with_sharding_constraint, BaseJAXAttentionModule, control_mlp_sharding

import chex

from .opt_configuration import OPTConfig
from ..easydel_modelling_utils import EasyDeLFlaxPretrainedModel
from fjformer.linen import Dense

logger = logging.get_logger(__name__)


# Copied from transformers.models.bart.modeling_flax_bart.FlaxBartAttention with Bart->OPT
class FlaxOPTAttention(BaseJAXAttentionModule):
    config: OPTConfig
    embed_dim: int
    num_heads: int
    dropout: float = 0.0
    causal: bool = False
    bias: bool = True
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self) -> None:
        self.head_dim = self.embed_dim // self.num_heads
        if self.head_dim * self.num_heads != self.embed_dim:
            raise ValueError(
                f"embed_dim must be divisible by num_heads (got `embed_dim`: {self.embed_dim}"
                f" and `num_heads`: {self.num_heads})."
            )

        dense = partial(
            Dense,
            self.embed_dim,
            use_bias=self.bias,
            dtype=self.dtype,
            kernel_init=jax.nn.initializers.normal(self.config.init_std),
        )

        self.q_proj, self.k_proj, self.v_proj = dense(), dense(), dense()
        self.out_proj = dense()

        self.dropout_layer = flax.linen.Dropout(rate=self.dropout)

        if self.causal:
            self.causal_mask = flax.linen.make_causal_mask(
                jnp.ones(
                    (1, getattr(self.config, "c_max_position_embeddings", self.config.max_position_embeddings)),
                    dtype="bool"
                ), dtype="bool"
            )

    def _split_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.num_heads, self.head_dim))

    def _merge_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.embed_dim,))

    def __call__(
            self,
            hidden_states: jnp.ndarray,
            key_value_states: Optional[jnp.ndarray] = None,
            attention_mask: Optional[jnp.ndarray] = None,
            init_cache: bool = False,
            deterministic: bool = True,
    ) -> Tuple[jnp.ndarray]:

        is_cross_attention = key_value_states is not None
        batch_size = hidden_states.shape[0]

        query_states = self.q_proj(hidden_states)

        if is_cross_attention:
            key_states = self.k_proj(key_value_states)
            value_states = self.v_proj(key_value_states)
        else:
            key_states = self.k_proj(hidden_states)
            value_states = self.v_proj(hidden_states)

        query_states = self._split_heads(query_states)
        key_states = self._split_heads(key_states)
        value_states = self._split_heads(value_states)
        # if self.config.use_sharding_constraint:
        #     query_states = with_sharding_constraint(
        #         query_states, PartitionSpec(("dp", "fsdp"), "sp" if query_states.shape != [1] else None, "tp", None)
        #     )
        #     key_states = with_sharding_constraint(key_states, PartitionSpec(("dp", "fsdp"), "sp", "tp", None))
        #     value_states = with_sharding_constraint(value_states, PartitionSpec(("dp", "fsdp"), "sp", "tp", None))
        if self.causal:
            query_length, key_length = query_states.shape[1], key_states.shape[1]
            if self.has_variable("cache", "cached_key"):
                mask_shift = self.variables["cache"]["cache_index"]
                max_decoder_length = self.variables["cache"]["cached_key"].shape[1]
                causal_mask = lax.dynamic_slice(
                    self.causal_mask, (0, 0, mask_shift, 0), (1, 1, query_length, max_decoder_length)
                )
            else:
                causal_mask = self.causal_mask[:, :, :query_length, :key_length]
            causal_mask = jnp.broadcast_to(causal_mask, (batch_size,) + causal_mask.shape[1:])

        # combine masks if needed
        if attention_mask is not None and self.causal:
            attention_mask = jnp.broadcast_to(jnp.expand_dims(attention_mask, axis=(-3, -2)), causal_mask.shape)
            attention_mask = combine_masks(attention_mask, causal_mask)
        elif self.causal:
            attention_mask = causal_mask
        elif attention_mask is not None:
            attention_mask = jnp.expand_dims(attention_mask, axis=(-3, -2))

        if self.causal and (self.has_variable("cache", "cached_key") or init_cache):
            key_states, value_states, attention_mask = self._concatenate_to_cache(
                key_states, value_states, query_states, attention_mask
            )
            if attention_mask is not None:
                attention_bias = lax.select(
                    attention_mask > 0,
                    jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
                    jnp.full(attention_mask.shape, jnp.finfo(self.dtype).min).astype(self.dtype),
                )
            else:
                attention_bias = None

            dropout_rng = None
            if not deterministic and self.dropout > 0.0:
                dropout_rng = self.make_rng("dropout")

            attn_weights = dot_product_attention_weights(
                query_states,
                key_states,
                bias=attention_bias,
                dropout_rng=dropout_rng,
                dropout_rate=self.dropout,
                broadcast_dropout=True,
                deterministic=deterministic,
                dtype=self.dtype,
                precision=None,
            )
            attn_output = jnp.einsum("...hqk,...khd->...qhd", attn_weights, value_states)
            attn_output = self._merge_heads(attn_output)
            if self.config.shard_attention_computation:
                attn_output = with_sharding_constraint(
                    attn_output, PartitionSpec(
                        ("dp", "fsdp"),
                        "sp" if attn_output.shape[1] != 1 else None,
                        "tp"
                    )
                )
            attn_output = self.out_proj(attn_output)

        return attn_output, attn_weights


class FlaxOPTDecoderLayer(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32

    def setup(self) -> None:
        self.embed_dim = self.config.hidden_size
        self.self_attn = FlaxOPTAttention(
            config=self.config,
            embed_dim=self.embed_dim,
            num_heads=self.config.num_attention_heads,
            dropout=self.config.attention_dropout,
            causal=True,
            dtype=self.dtype,
        )
        self.do_layer_norm_before = self.config.do_layer_norm_before
        self.dropout_layer = flax.linen.Dropout(rate=self.config.dropout)
        self.activation_fn = ACT2FN[self.config.activation_function]

        self.self_attn_layer_norm = nn.LayerNorm(dtype=self.dtype, epsilon=1e-05)
        self.fc1 = Dense(
            self.config.ffn_dim,
            dtype=self.dtype,
            kernel_init=jax.nn.initializers.normal(self.config.init_std),
        )
        self.fc2 = Dense(
            self.embed_dim, dtype=self.dtype, kernel_init=jax.nn.initializers.normal(self.config.init_std)
        )
        self.final_layer_norm = nn.LayerNorm(dtype=self.dtype, epsilon=1e-05)

    def __call__(
            self,
            hidden_states: jnp.ndarray,
            attention_mask: jnp.ndarray,
            init_cache: bool = False,
            output_attentions: bool = True,
            deterministic: bool = True,
    ) -> Tuple[jnp.ndarray]:
        residual = hidden_states

        # 125m, 1.7B, ..., 175B applies layer norm BEFORE attention
        if self.do_layer_norm_before:
            hidden_states = self.self_attn_layer_norm(hidden_states)

        # Self Attention
        hidden_states, self_attn_weights = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            init_cache=init_cache,
            deterministic=deterministic,
        )
        hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)
        hidden_states = residual + hidden_states
        hidden_states = control_mlp_sharding(hidden_states, self.config.partition_axis)
        # 350m applies layer norm AFTER attention
        if not self.do_layer_norm_before:
            hidden_states = self.self_attn_layer_norm(hidden_states)

        # Fully Connected
        hidden_states_shape = hidden_states.shape
        hidden_states = hidden_states.reshape(-1, hidden_states.shape[-1])
        residual = hidden_states

        # 125m, 1.7B, ..., 175B applies layer norm BEFORE attention
        if self.do_layer_norm_before:
            hidden_states = self.final_layer_norm(hidden_states)

        hidden_states = self.fc1(hidden_states)
        hidden_states = self.activation_fn(hidden_states)

        hidden_states = self.fc2(hidden_states)
        hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)

        hidden_states = (residual + hidden_states).reshape(hidden_states_shape)

        # 350m applies layer norm AFTER attention
        if not self.do_layer_norm_before:
            hidden_states = self.final_layer_norm(hidden_states)

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        return outputs


class FlaxOPTDecoderLayerCollection(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        block = FlaxOPTDecoderLayer
        if self.config.gradient_checkpointing != "":
            block = nn.remat(
                block,
                static_argnums=(3, 4),
                policy=get_gradient_checkpoint_policy(self.config.gradient_checkpointing)
            )
        self.layers = [
            block(self.config, name=str(i), dtype=self.dtype)
            for i in range(self.config.num_hidden_layers)
        ]
        self.layerdrop = self.config.layerdrop

    def __call__(
            self,
            hidden_states,
            attention_mask,
            deterministic: bool = True,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
    ):
        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None

        for decoder_layer in self.layers:
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            layer_outputs = decoder_layer(
                hidden_states,
                attention_mask=attention_mask,
                init_cache=init_cache,
                output_attentions=output_attentions,
                deterministic=deterministic,
            )

            hidden_states = layer_outputs[0]
            if output_attentions:
                all_self_attns += (layer_outputs[1],)

        outputs = [hidden_states, all_hidden_states, all_self_attns]
        return outputs


class FlaxOPTLearnedPositionalEmbedding(nn.Embed):

    def setup(self):
        self.offset = 2
        self.embedding = self.param(
            "embedding", self.embedding_init, (self.num_embeddings + self.offset, self.features), self.param_dtype
        )

    def __call__(self, positions):
        """`input_ids_shape` is expected to be [bsz x seqlen]."""

        return super().__call__(positions + self.offset)


class FlaxOPTDecoder(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation
    offset: int = 2

    def setup(self):
        self.dropout_layer = flax.linen.Dropout(rate=self.config.dropout)

        embed_dim = self.config.hidden_size
        self.padding_idx = self.config.pad_token_id
        self.max_target_positions = self.config.max_position_embeddings

        self.embed_tokens = nn.Embed(
            self.config.vocab_size,
            self.config.word_embed_proj_dim,
            embedding_init=jax.nn.initializers.normal(self.config.init_std),
            dtype=self.dtype,
        )

        self.embed_positions = FlaxOPTLearnedPositionalEmbedding(
            self.config.max_position_embeddings,
            embed_dim,
            embedding_init=jax.nn.initializers.normal(self.config.init_std),
            dtype=self.dtype,
        )

        if self.config.word_embed_proj_dim != self.config.hidden_size:
            self.project_in = Dense(self.config.hidden_size, use_bias=False)
            self.project_out = Dense(self.config.word_embed_proj_dim, use_bias=False)

        else:
            self.project_in = None
            self.project_out = None

        if self.config.do_layer_norm_before and not self.config._remove_final_layer_norm:
            self.final_layer_norm = nn.LayerNorm(dtype=self.dtype, epsilon=1e-05)
        else:
            self.final_layer_norm = None

        self.layers = FlaxOPTDecoderLayerCollection(self.config, self.dtype)

    def __call__(
            self,
            input_ids,
            attention_mask,
            position_ids,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            return_dict: bool = True,
            deterministic: bool = True,
    ):
        input_shape = input_ids.shape
        input_ids = input_ids.reshape(-1, input_shape[-1])

        inputs_embeds = self.embed_tokens(input_ids)
        if self.project_in is not None:
            inputs_embeds = self.project_in(inputs_embeds)

        positions = self.embed_positions(position_ids)

        hidden_states = inputs_embeds + positions

        hidden_state, all_hidden_states, attentions = self.layers(
            hidden_states,
            attention_mask,
            deterministic=deterministic,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
        )

        if self.final_layer_norm is not None:
            hidden_state = self.final_layer_norm(hidden_state)

        if self.project_out is not None:
            hidden_state = self.project_out(hidden_state)

        if output_hidden_states:
            all_hidden_states += (hidden_state,)

        outputs = [hidden_state, all_hidden_states, attentions]

        if not return_dict:
            return tuple(v for v in outputs if v is not None)

        return FlaxBaseModelOutput(
            last_hidden_state=hidden_state,
            hidden_states=all_hidden_states,
            attentions=attentions,
        )


class FlaxOPTPreTrainedModel(EasyDeLFlaxPretrainedModel):
    config_class = OPTConfig
    base_model_prefix: str = "model"
    module_class: nn.Module = None

    def __init__(
            self,
            config: OPTConfig,
            input_shape: Tuple[int] = (1, 1),
            seed: int = 0,
            dtype: jnp.dtype = jnp.float32,
            _do_init: bool = True,
            **kwargs,
    ):
        module = self.module_class(config=config, dtype=dtype, **kwargs)
        super().__init__(config, module, input_shape=input_shape, seed=seed, dtype=dtype, _do_init=_do_init)

    def init_weights(self, rng: jax.random.PRNGKey, input_shape: Tuple, params: FrozenDict = None) -> FrozenDict:
        # init input tensors
        input_ids = jnp.zeros(input_shape, dtype="i4")
        attention_mask = jnp.ones_like(input_ids)

        batch_size, sequence_length = input_ids.shape
        position_ids = jnp.broadcast_to(jnp.arange(sequence_length)[None, :], (batch_size, sequence_length))

        params_rng, dropout_rng = jax.random.split(rng)
        rngs = {"params": params_rng, "dropout": dropout_rng}

        module_init_outputs = self.module.init(
            rngs,
            input_ids,
            attention_mask,
            position_ids,
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

        input_ids = jnp.ones((batch_size, max_length), dtype="i4")
        attention_mask = jnp.ones_like(input_ids, dtype="i4")
        position_ids = jnp.broadcast_to(jnp.arange(jnp.atleast_2d(input_ids).shape[-1]), input_ids.shape)

        init_variables = self.module.init(
            jax.random.PRNGKey(0), input_ids, attention_mask, position_ids, return_dict=False, init_cache=True
        )
        return unfreeze(init_variables["cache"])

    def __call__(
            self,
            input_ids: jnp.ndarray,
            attention_mask: Optional[jnp.ndarray] = None,
            position_ids: Optional[jnp.ndarray] = None,
            params: dict = None,
            past_key_values: dict = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
            dropout_rng: PRNGKey = None,
            deterministic: bool = True,
            **kwargs
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        if attention_mask is None:
            attention_mask = jnp.ones_like(input_ids)

        if position_ids is None:
            position_ids = (attention_mask.cumsum(axis=1) * attention_mask) - 1

        # Handle any PRNG if needed
        rngs = {"dropout": dropout_rng} if dropout_rng is not None else {}

        inputs = {"params": params or self.params}

        if past_key_values is not None:
            inputs["cache"] = past_key_values
            mutable = ["cache"]
        else:
            mutable = False

        outputs = self.module.apply(
            inputs,
            input_ids=jnp.array(input_ids, dtype="i4"),
            attention_mask=jnp.array(attention_mask, dtype="i4"),
            position_ids=jnp.array(position_ids, dtype="i4"),
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
            rngs=rngs,
            mutable=mutable,
        )

        # add updated cache to model output
        if past_key_values is not None and return_dict:
            outputs, past_key_values = outputs
            outputs["past_key_values"] = unfreeze(past_key_values["cache"])
            return outputs
        elif past_key_values is not None and not return_dict:
            outputs, past_key_values = outputs
            outputs = outputs[:1] + (unfreeze(past_key_values["cache"]),) + outputs[1:]

        return outputs


class FlaxOPTModule(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        self.decoder = FlaxOPTDecoder(self.config, dtype=self.dtype)

    def _get_decoder_module(self):
        return self.decoder

    def __call__(
            self,
            input_ids,
            attention_mask,
            position_ids,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            return_dict: bool = True,
            deterministic: bool = True,
            init_cache=False,
    ):
        decoder_outputs = self.decoder(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
            init_cache=init_cache,
        )

        if not return_dict:
            return decoder_outputs

        return FlaxBaseModelOutput(
            last_hidden_state=decoder_outputs.last_hidden_state,
            hidden_states=decoder_outputs.hidden_states,
            attentions=decoder_outputs.attentions,
        )


class FlaxOPTModel(FlaxOPTPreTrainedModel):
    module_class = FlaxOPTModule

    def set_input_embeddings(self, value):
        self.module.embed_tokens = value

    def get_input_embeddings(self):
        return self.module.embed_tokens


class FlaxOPTForCausalLMModule(nn.Module):
    config: OPTConfig
    dtype: jnp.dtype = jnp.float32

    def setup(self):
        self.model = FlaxOPTModule(config=self.config, dtype=self.dtype)
        self.lm_head = Dense(
            self.config.vocab_size,
            use_bias=False,
            dtype=self.dtype,
            kernel_init=jax.nn.initializers.normal(self.config.init_std),
        )

    def __call__(
            self,
            input_ids,
            attention_mask,
            position_ids,
            init_cache: bool = False,
            output_attentions: bool = False,
            output_hidden_states: bool = False,
            return_dict: bool = True,
            deterministic: bool = True,
    ):
        outputs = self.model(
            input_ids,
            attention_mask,
            position_ids,
            init_cache=init_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
        )

        hidden_states = outputs[0]

        if self.config.tie_word_embeddings:
            shared_kernel = self.model.variables["params"]["decoder"]["embed_tokens"]["embedding"]
            shared_kernel = fjformer.linen.control_quantization(shared_kernel, self.param_dtype).T
            lm_logits = self.lm_head.apply({"params": {"kernel": shared_kernel}}, hidden_states)
        else:
            lm_logits = self.lm_head(hidden_states)

        if not return_dict:
            return (lm_logits,) + outputs[1:]

        return FlaxMaskedLMOutput(
            logits=lm_logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


class FlaxOPTForCausalLM(FlaxOPTPreTrainedModel):
    module_class = FlaxOPTForCausalLMModule

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

    def prepare_inputs_for_generation(self, input_ids, max_length, attention_mask: Optional[chex.Array] = None):
        # initializing the cache
        batch_size, seq_length = input_ids.shape

        past_key_values = self.init_cache(batch_size, max_length)
        # Note that usually one would have to put 0's in the attention_mask for x > input_ids.shape[-1] and x < cache_length.
        # But since the decoder uses a causal mask, those positions are masked anyway.
        # Thus, we can create a single static attention_mask here, which is more efficient for compilation
        extended_attention_mask = jnp.ones((batch_size, max_length), dtype="i4")

        if attention_mask is not None:
            position_ids = attention_mask.cumsum(axis=1) - 1
            extended_attention_mask = lax.dynamic_update_slice(extended_attention_mask, attention_mask, (0, 0))
        else:
            position_ids = jnp.broadcast_to(jnp.arange(seq_length, dtype="i4")[None, :], (batch_size, seq_length))

        return {
            "past_key_values": past_key_values,
            "attention_mask": extended_attention_mask,
            "position_ids": position_ids,
        }

    def update_inputs_for_generation(self, model_outputs, model_kwargs):
        model_kwargs["past_key_values"] = model_outputs.past_key_values
        model_kwargs["position_ids"] = model_kwargs["position_ids"][:, -1:] + 1
        return model_kwargs
