# Custom Airflow image for the Predictive Pipeline Guardian.
# Adds the JDK (for Spark) + Kafka/pyspark clients on top of Airflow 2.10.5.
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
