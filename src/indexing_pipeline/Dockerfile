# In case if any tool not available now
FROM athithya5354/indexing_pipeline_cpu:amd64-arm64-v7 AS base

ENV PYTHONUNBUFFERED=1 \
    VENV_PATH=/opt/venv \
    PATH=${VENV_PATH}/bin:$PATH \
    PYTHONPATH=/indexing_pipeline \
    PORT=8000 \
    HEALTH_PATH=/health

WORKDIR /indexing_pipeline
COPY . /indexing_pipeline

COPY pre_conversions.sh /indexing_pipeline/pre_conversions.sh
RUN chmod +x /indexing_pipeline/pre_conversions.sh

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/venv/bin/python", "indexing_pipeline.py"]
