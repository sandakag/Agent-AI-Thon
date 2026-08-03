"""Predictive Pipeline Guardian — a REAL Apache Airflow DAG.

    trades-raw (Kafka)  --EXTRACT-->  ETL transform+load  --SIGNALS-->
      predictive agent (GitHub Copilot brain)  --> preventive policy
      --> early warning + REAL predicted-incident issue / gated preventive PR

Each stage is a task you watch go green in the Airflow UI. The guardian
predicts a failure BEFORE it happens, opens a governed ticket while the pipeline
is still healthy (lead time), and stages a gated fix for a human to approve.
Nothing is ever auto-merged.

Trigger DAG w/ config (from the Airflow UI):
    {}                            # healthy live data -> stays GREEN
    {"inject": "schema-drift"}    # ramps each run until risk crosses AMBER -> RED
    {"inject": "null-surge"}      # missing quantity -> null amounts
    {"inject": "volume-drop"}     # upstream stall / starvation
    {"inject": "latency-surge"}   # load climbs -> latency SLA breach -> processing timeout
    {"reset": true}               # clear the ramp + banner
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

import pendulum

PROJECT_ROOT = os.environ.get("ETL_PROJECT_ROOT", "/opt/airflow/project")
if PROJECT_ROOT and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from airflow import DAG
from airflow.operators.python import PythonOperator

import predictive_logic as pl


def _conf(context) -> dict:
    dag_run = context.get("dag_run")
    return (dag_run.conf if dag_run and dag_run.conf else {}) or {}


def _extract(**context):
    out = pl.do_extract(_conf(context), context["run_id"])
    context["ti"].xcom_push(key="raw", value=out["raw"])
    return {"count": len(out["raw"]), "mode": out["mode"], "tick": out["tick"]}


def _transform_load(**context):
    raw = context["ti"].xcom_pull(key="raw", task_ids="extract_trades") or []
    out = pl.do_transform_load(raw, context["run_id"])
    context["ti"].xcom_push(key="signals", value=out["signals"])
    context["ti"].xcom_push(key="etl_failed", value=out["etl_failed"])
    context["ti"].xcom_push(key="etl_error", value=out["etl_error"])
    return {"etl_failed": out["etl_failed"]}


def _predict(**context):
    prediction = pl.do_predict(context["run_id"])
    context["ti"].xcom_push(key="prediction", value=prediction)
    return {"risk": prediction.get("risk_score"), "type": prediction.get("predicted_failure_type")}


def _govern(**context):
    ti = context["ti"]
    prediction = ti.xcom_pull(key="prediction", task_ids="predict_risk") or {}
    signals = ti.xcom_pull(key="signals", task_ids="transform_load") or {}
    etl_failed = bool(ti.xcom_pull(key="etl_failed", task_ids="transform_load"))
    etl_error = ti.xcom_pull(key="etl_error", task_ids="transform_load")
    decision = pl.do_govern(prediction, etl_failed, etl_error, signals, context["run_id"])
    return {"level": decision["level"]}


default_args = {
    "owner": "predictive-guardian",
    "retries": 1,
    "retry_delay": timedelta(seconds=10),
}

with DAG(
    dag_id="predictive_pipeline_guardian",
    description="Predict pipeline failures BEFORE they happen (GitHub Copilot brain)",
    default_args=default_args,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    schedule="*/10 * * * *",
    catchup=False,
    max_active_runs=1,
    tags=["predictive", "ai", "kafka", "spark", "copilot", "governance"],
    doc_md=__doc__,
) as dag:
    extract_trades = PythonOperator(task_id="extract_trades", python_callable=_extract)
    transform_load = PythonOperator(task_id="transform_load", python_callable=_transform_load)
    predict_risk = PythonOperator(task_id="predict_risk", python_callable=_predict)
    govern = PythonOperator(task_id="govern", python_callable=_govern)

    extract_trades >> transform_load >> predict_risk >> govern
