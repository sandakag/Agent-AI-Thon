# Custom Airflow image for the Predictive Pipeline Guardian.
# Adds the JDK (for Spark) + Kafka/pyspark clients on top of Airflow 2.10.5.
# Includes OpenTelemetry zero-code auto-instrumentation so services can be
# wrapped with `opentelemetry-instrument` without any source-code changes.
FROM apache/airflow:2.10.5

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends default-jdk-headless procps curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH="${JAVA_HOME}/bin:${PATH}"

USER airflow
# kafka-python-ng is the maintained fork that works on modern Python (3.12+).
RUN pip install --no-cache-dir --retries 10 --timeout 120 kafka-python-ng==2.2.3

# pyspark is a ~317MB download; on restricted networks it can truncate. It is
# OPTIONAL — the TRANSFORM stage falls back to a pure-Python aggregation — so we
# install it best-effort and never fail the build if it can't be fetched.
RUN pip install --no-cache-dir --retries 10 --timeout 120 pyspark==3.5.1 \
    || echo "WARN: pyspark not installed (network); using pure-Python transform fallback."

# --- OpenTelemetry zero-code auto-instrumentation ---
# opentelemetry-distro  : SDK + opentelemetry-instrument CLI wrapper
# opentelemetry-exporter-otlp-proto-http : OTLP/HTTP exporter (talks to collector)
# opentelemetry-bootstrap -a install     : detects installed libraries (kafka-python,
#   urllib, logging) and installs the matching instrumentation packages so that
#   wrapping a service with `opentelemetry-instrument python app.py` captures
#   traces, metrics, and logs with zero source-code changes.
RUN pip install --no-cache-dir --retries 10 --timeout 120 \
    "opentelemetry-distro==0.48b0" \
    "opentelemetry-exporter-otlp-proto-http==1.27.0" \
    && opentelemetry-bootstrap -a install
