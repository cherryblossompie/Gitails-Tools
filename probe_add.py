"""Probe addendum 82-108."""
import pathlib
from gitail.semantics import parse_text, load_config_dir
cfg, _ = load_config_dir(pathlib.Path("Gitails-Tools/config"))
for s in ["NOM. 50MM SLAB SETDOWN", "1:100 FALL", "CONCRETE SLAB REFER ENG. DWGS FOR DETAILS", "NOM. FALL"]:
    print(repr(s), "->", parse_text(s, cfg))
from gitail.ai import validate_partition, AIInvalidOutput
print("partition ok", validate_partition([[0, 1], [2]], 3))
try:
    validate_partition([[0, 1], [1, 2]], 3)
    print("BAD accepted")
except AIInvalidOutput as e:
    print("non-partition rejected OK")