"""Sinal de vida do agente para o monitoramento do Portal Bindi Log (cabeçalho
"Agentes RPA"): avisa POST /api/rpa/monitoramento no início e no fim de cada
execução. A execução conta como erro se levantar exceção ou imprimir qualquer
linha "[ERRO..." (padrão de log Priortec), mesmo que o erro tenha sido tratado
por item. Falha ao avisar o portal nunca interrompe o bot - só gera [AVISO].

Mesmo arquivo nos agentes Bindi - Vibra, Bindi - Cadastro, Bind-Rpa,
Bindi - Cadastro Ipiranga e Bindi - Shell: alterar em todos."""

import math
import os
import sys
from contextlib import contextmanager
from typing import Iterator, Optional

import requests
from dotenv import load_dotenv

REQUEST_TIMEOUT_SECONDS = 15
# Agentes que rodam uma vez por disparo do cron do Railway (SCHEDULE_INTERVAL_MINUTES=0)
# usam o intervalo do cron (*/30), que o bot não tem como ler: MONITOR_INTERVAL_MINUTES sobrescreve.
DEFAULT_CRON_INTERVAL_MINUTES = 30
MAX_MESSAGE_LENGTH = 1000


class _ErrorLineCounter:
    """Repassa tudo ao stdout original e conta as linhas que começam com "[ERRO"."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.error_count = 0
        self.last_error: Optional[str] = None
        self._pending = ""

    def write(self, text: str) -> int:
        self.stream.write(text)
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._inspect(line)
        return len(text)

    def finish(self) -> None:
        self._inspect(self._pending)
        self._pending = ""

    def _inspect(self, line: str) -> None:
        stripped = line.strip()
        if stripped.startswith("[ERRO"):
            self.error_count += 1
            self.last_error = stripped

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _expected_interval_minutes() -> int:
    for name in ("SCHEDULE_INTERVAL_MINUTES", "MONITOR_INTERVAL_MINUTES"):
        raw_value = os.environ.get(name, "").strip().replace(",", ".")
        try:
            minutes = float(raw_value) if raw_value else 0
        except ValueError:
            minutes = 0
        if minutes > 0:
            return max(1, math.ceil(minutes))
    return DEFAULT_CRON_INTERVAL_MINUTES


def _send(agent_code: str, agent_name: str, event: str, success: Optional[bool], message: Optional[str]) -> None:
    base_url = os.environ.get("PORTAL_API_URL", "").strip()
    token = os.environ.get("PORTAL_API_TOKEN", "").strip()
    if not base_url or not token:
        print("[AVISO] Monitoramento desligado: PORTAL_API_URL/PORTAL_API_TOKEN não definidos.", file=sys.__stdout__)
        return

    url = f"{base_url.rstrip('/')}/api/rpa/monitoramento"
    body = {
        "agentCode": agent_code,
        "agentName": agent_name,
        "event": event,
        "expectedIntervalMinutes": _expected_interval_minutes(),
        "message": message[:MAX_MESSAGE_LENGTH] if message else None,
    }
    if success is not None:
        body["success"] = success
    try:
        response = requests.post(
            url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if response.status_code != 200:
            print(
                f"[AVISO] Portal Bindi Log recusou o sinal de monitoramento ({event}, HTTP {response.status_code}): "
                f"{response.text[:300]}",
                file=sys.__stdout__,
            )
    except requests.RequestException as exc:
        print(f"[AVISO] Falha ao enviar o sinal de monitoramento ({event}) ao Portal Bindi Log: {exc}", file=sys.__stdout__)


@contextmanager
def monitored_run(agent_code: str, agent_name: str) -> Iterator[None]:
    """Envolve uma execução do pipeline: sinal 'started' antes, 'finished' (ok/erro) depois."""
    # Os agentes que carregam o .env só no InitModule precisam dele já aqui.
    load_dotenv()
    _send(agent_code, agent_name, "started", None, None)

    counter = _ErrorLineCounter(sys.stdout)
    sys.stdout = counter
    exception_message: Optional[str] = None
    try:
        yield
    except BaseException as exc:
        exception_message = f"Execução interrompida: {type(exc).__name__}: {exc}"
        raise
    finally:
        counter.finish()
        sys.stdout = counter.stream
        success = exception_message is None and counter.error_count == 0
        if exception_message:
            message = exception_message
        elif counter.error_count:
            message = f"{counter.error_count} erro(s) na execução. Último: {counter.last_error}"
        else:
            message = None
        _send(agent_code, agent_name, "finished", success, message)
