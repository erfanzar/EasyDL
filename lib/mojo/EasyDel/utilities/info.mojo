from sys.info import (
    os_is_linux,
    os_is_windows,
    os_is_macos,
    has_sse4,
    has_avx,
    has_avx2,
    has_avx512f,
    has_neon,
    is_apple_m1,
    has_intel_amx,
    _current_target,
    _current_cpu,
    _triple_attr,
    
)
from runtime.llcl import num_cores


fn system_information() -> Tuple[StringLiteral, String, String, Int, String]:
    r"""
    Return os, cpu, arch, num_cores and cpu_features.
    """
    var os = ""
    if os_is_linux():
        os = "Linux"
    elif os_is_macos():
        os = "macOS"
    else:
        os = "windows"
    let cpu = String(_current_cpu())
    let arch = String(_triple_attr())
    var cpu_features = String(" ")
    if has_sse4():
        cpu_features = cpu_features.join(" sse4")
    if has_avx():
        cpu_features = cpu_features.join(" avx")
    if has_avx2():
        cpu_features = cpu_features.join(" avx2")
    if has_avx512f():
        cpu_features = cpu_features.join(" avx512f")
    if has_intel_amx():
        cpu_features = cpu_features.join(" intel_amx")
    if has_neon():
        cpu_features = cpu_features.join(" neon")
    if is_apple_m1():
        cpu_features = cpu_features.join(" Apple M1")

    return os, cpu, arch, num_cores(), cpu_features
