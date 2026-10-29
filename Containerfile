FROM docker.io/library/python:3.13-alpine
COPY jukebox_sidecar.py /app/jukebox_sidecar.py
USER nobody
ENV LISTEN=0.0.0.0:4534
EXPOSE 4534
CMD ["python3", "-u", "/app/jukebox_sidecar.py"]
