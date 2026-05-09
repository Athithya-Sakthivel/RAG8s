# In case if any tool not available now
FROM ghcr.io/athithya-sakthivel/indexing-pipeline-base:2026-05-09-09-21@sha256:a5355bd88e955c5ecd4360b183f35e94c689abb47f2e6968b22f0a9e6aaf75f3

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
