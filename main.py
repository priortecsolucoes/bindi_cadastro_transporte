"""Orquestrador do bot de Programação FOB do Canal de Negócios Vibra.
Pipeline FrameworkRE: INIT -> GET_DATA -> PROCESS -> END, repetido a cada
SCHEDULE_INTERVAL_MINUTES (0 = executa uma vez e encerra)."""

import os
import time
import traceback
from datetime import datetime, timedelta

from dotenv import load_dotenv

from Framework import EndModule, GetDataModule, InitModule, ProcessModule
from Framework.ProcessModule import short_error
from Framework.RPAFrameworkException import RPAFrameworkException
from Log.LogModule import LogModule

DEFAULT_SCHEDULE_INTERVAL_MINUTES = 5


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


def _schedule_interval_minutes() -> float:
    raw_value = os.environ.get("SCHEDULE_INTERVAL_MINUTES", "").strip() or str(DEFAULT_SCHEDULE_INTERVAL_MINUTES)
    try:
        minutes = float(raw_value.replace(",", "."))
    except ValueError as exc:
        raise RPAFrameworkException(
            f"SCHEDULE_INTERVAL_MINUTES deve ser um número (minutos); valor recebido: {raw_value!r}"
        ) from exc
    if minutes < 0:
        raise RPAFrameworkException(f"SCHEDULE_INTERVAL_MINUTES não pode ser negativo; valor recebido: {raw_value!r}")
    return minutes


def main() -> None:
    load_dotenv()
    try:
        interval_minutes = _schedule_interval_minutes()
    except RPAFrameworkException as exc:
        print(f"[ERRO Framework] {exc}")
        return

    if interval_minutes == 0:
        run()
        return

    print(f"[INFO] Agendamento ativo: executando a cada {interval_minutes:g} min (Ctrl+C para parar).")
    cycle = 0
    try:
        while True:
            cycle += 1
            started_at = datetime.now()
            print("=" * 55)
            print(f"[INFO] Ciclo {cycle} iniciado em {started_at:%d/%m/%Y %H:%M:%S}")
            try:
                run()
            except Exception as exc:  # noqa: BLE001 - um ciclo com erro inesperado não pode matar o agendamento
                # Erros conhecidos já viram [ERRO]/[ERRO Framework] curtos; aqui só chega bug - o traceback ajuda a achar.
                print(f"[ERRO] Ciclo {cycle} interrompido por erro inesperado: {short_error(exc)}")
                print(f"[INFO] Detalhes técnicos do erro:\n{traceback.format_exc()}")

            # O intervalo conta do início do ciclo; se o ciclo demorou mais que ele, o próximo começa já.
            next_run = started_at + timedelta(minutes=interval_minutes)
            wait_seconds = max(0.0, (next_run - datetime.now()).total_seconds())
            print(f"[INFO] Próximo ciclo às {max(next_run, datetime.now()):%H:%M:%S}.")
            time.sleep(wait_seconds)
    except KeyboardInterrupt:
        print("\n[INFO] Agendamento interrompido pelo usuário (Ctrl+C).")


if __name__ == "__main__":
    main()
