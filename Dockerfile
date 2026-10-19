FROM python:3.12-slim
WORKDIR /app
COPY review_pulse.py LICENSE README.md ./
RUN useradd -m pulse && mkdir /data && chown pulse /data
USER pulse
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 CHECK_EVERY_MINUTES=15
VOLUME /data
CMD ["python", "review_pulse.py"]
