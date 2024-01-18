import copy
import jax
import transformers
from flax.traverse_util import flatten_dict, unflatten_dict

try:
    from lib.python.EasyDel.modules.phi import PhiConfig, FlaxPhiForCausalLM
    from lib.python.EasyDel.transform.easydel_transform import huggingface_to_easydel
except ModuleNotFoundError:
    import sys
    from pathlib import Path

    cp = Path.cwd().__str__()
    sys.path.append(cp)
    from lib.python.EasyDel.modules.phi import PhiConfig, FlaxPhiForCausalLM
    from lib.python.EasyDel.transform.easydel_transform import huggingface_to_easydel
from jax import numpy as jnp
from transformers import AutoModelForCausalLM, AutoConfig
import torch
import numpy as np


def main():
    torch.manual_seed(42)

    torch_config = AutoConfig.from_pretrained(
        'microsoft/phi-2',
        trust_remote_code=True
    )
    torch_config.hidden_size = 256
    torch_config.intermediate_size = 512
    torch_config.max_position_embeddings = 128
    torch_config.num_hidden_layers = 4
    torch_config.num_key_value_heads = 4
    torch_config.num_attention_heads = 8

    torch_model = AutoModelForCausalLM.from_config(
        config=torch_config,
        trust_remote_code=True
    )

    params = {
        "params": huggingface_to_easydel(
            torch_model.state_dict(),
            embedding_layer_names=["embed_tokens"],
            device=jax.devices("cpu")[0],
            layer_norm_names=[
                "input_layernorm",
                "final_layernorm",
                "q_layernorm",
                "k_layernorm"
            ]
        )
    }
    print(params)
    np_random_input_ids = np.random.randint(0, torch_config.vocab_size, (1, 128))
    input_ids = torch.from_numpy(np_random_input_ids).reshape(1, -1).to(torch.long)
    flax_input_ids = jnp.asarray(np_random_input_ids, dtype=jnp.int32).reshape(1, -1)

    torch_output = torch_model(
        input_ids=input_ids
    )
    config = PhiConfig()
    for k, v in torch_config.__dict__.items():
        setattr(config, k, v)
    config.add_jax_args()
    flax_model = FlaxPhiForCausalLM(
        config=config,
        dtype=jnp.float32,
        param_dtype=jnp.float32,
        _do_init=False,
        input_shape=(1, 6)
    )
    flax_output = flax_model(
        input_ids=flax_input_ids,
        params=params,
        add_params_field=False,
        train=False
    )
    torch_output = torch_output.logits.cpu().detach().numpy()
    res = jnp.allclose(torch_output, flax_output.logits, atol=1e-5)
    print('PHI Huggingface Predictions :\n', torch_output,
          '\nEasyDel Predictions: \n', flax_output.logits)
    if res:
        print('\033[1;36mTest Passed Unfortunately 🥳')
    else:
        print('\033[1;31mTest Failed Successfully  🤕')
    print()


if __name__ == '__main__':
    main()
