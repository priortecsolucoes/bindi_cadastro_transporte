"""Orquestrador do bot de Programação FOB do Canal de Negócios Vibra.
Pipeline FrameworkRE: INIT -> GET_DATA -> PROCESS -> END."""

from Framework import EndModule, GetDataModule, InitModule, ProcessModule
from Framework.RPAFrameworkException import RPAFrameworkException
from Log.LogModule import LogModule


def run() -> None:
    log_module = None
    try:
        config = InitModule.initialize()
        items = GetDataModule.get_data(config)
        log_module = LogModule()
        ProcessModule.process_items(items, config, log_module)
    except RPAFrameworkException as exc:
        print(f"[ERRO Framework] {exc}")
    finally:
        EndModule.finalize(log_module)


if __name__ == "__main__":
    run()
