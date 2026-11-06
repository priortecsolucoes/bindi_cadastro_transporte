"""END — executado no finally de Main.run(). Fecha recursos e imprime o resumo."""

from typing import Optional

from Log.LogModule import LogModule


def finalize(log_module: Optional[LogModule]) -> None:
    if log_module is not None:
        log_module.close()
    print("=" * 55)
    print("[OK] Programação FOB Vibra finalizada.")
