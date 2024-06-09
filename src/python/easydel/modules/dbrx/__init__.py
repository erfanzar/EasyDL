from ...utils.lazy_import import _LazyModule
from typing import TYPE_CHECKING

_import_structure = {
    "dbrx_configuration": ["DbrxConfig", "DbrxFFNConfig", "DbrxAttentionConfig"],
    "modelling_dbrx_flax": ["FlaxDbrxModel", "FlaxDbrxForCausalLM"],
}

if TYPE_CHECKING:
    from .dbrx_configuration import (
        DbrxConfig as DbrxConfig,
        DbrxFFNConfig as DbrxFFNConfig,
        DbrxAttentionConfig as DbrxAttentionConfig,
    )
    from .modelling_dbrx_flax import (
        FlaxDbrxModel as FlaxDbrxModel,
        FlaxDbrxForCausalLM as FlaxDbrxForCausalLM,
    )
else:
    import sys

    sys.modules[__name__] = _LazyModule(
        __name__, globals()["__file__"], _import_structure, module_spec=__spec__
    )
