"""LogModule — único módulo que persiste o resultado local das programações.
Mantém o CSV de saída aberto durante toda a execução. Fecha no EndModule.
O CSV é acumulado entre execuções: é o histórico que o GetDataModule usa para
não cadastrar de novo na Vibra uma viagem já FINALIZADA."""

import csv
import os
from collections import Counter
from datetime import datetime
from typing import Optional

from Framework.RPAFrameworkException import RPAFrameworkException

STATUS_FILLED = "PREENCHIDO"
STATUS_FINALIZED = "FINALIZADO"
STATUS_ERROR = "ERRO"
DEFAULT_OUTPUT_PATH = "programacoes_vibra.csv"


def output_path() -> str:
    """PROGRAMACOES_OUTPUT_PATH (no Railway, num Volume em /data) - lido na hora,
    porque o .env só é carregado depois dos imports."""
    return os.environ.get("PROGRAMACOES_OUTPUT_PATH", "").strip() or DEFAULT_OUTPUT_PATH


FIELDNAMES = [
    "processed_at", "trip_id", "load_date", "load_time", "order_numbers", "distributor_order_numbers", "pool", "plate", "carrier", "selected_pool", "selected_carrier", "status", "detail",
]


class LogModule:
    def __init__(self, path: Optional[str] = None) -> None:
        self._output_path = path or output_path()
        is_new_file = not os.path.isfile(self._output_path) or os.path.getsize(self._output_path) == 0
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._output_path)), exist_ok=True)
            self._file = open(self._output_path, "a", newline="", encoding="utf-8")
        except OSError as exc:
            raise RPAFrameworkException(
                f"Não foi possível abrir {self._output_path} para escrita (aberto no Excel?): {exc}"
            ) from exc
        self._writer = csv.DictWriter(self._file, fieldnames=FIELDNAMES)
        if is_new_file:
            self._writer.writeheader()
        self._status_count: Counter = Counter()

    def write_result(
        self,
        item: dict,
        status: str,
        selected_pool: str = "",
        selected_carrier: str = "",
        detail: str = "",
    ) -> None:
        self._writer.writerow(
            {
                "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "trip_id": item.get("tripId", ""),
                "load_date": item.get("loadDate", ""),
                "load_time": item.get("loadTime", ""),
                "order_numbers": " ".join(o["orderNumber"] for o in item.get("orders", [])),
                "distributor_order_numbers": " ".join(o["distributorOrderNumber"] for o in item.get("orders", [])),
                "pool": item["pool"],
                "plate": item["plate"],
                "carrier": item["carrier"],
                "selected_pool": selected_pool,
                "selected_carrier": selected_carrier,
                "status": status,
                "detail": detail,
            }
        )
        self._file.flush()
        self._status_count[status] += 1

    def close(self) -> None:
        self._file.close()
        total = sum(self._status_count.values())
        print(
            f"[OK] {total} programação(ões) desta execução gravada(s) em {self._output_path} "
            f"({self._status_count[STATUS_FINALIZED]} finalizada(s), {self._status_count[STATUS_FILLED]} só preenchida(s), "
            f"{self._status_count[STATUS_ERROR]} com erro)."
        )
