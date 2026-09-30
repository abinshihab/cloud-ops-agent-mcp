FROM python:3.11-slim
WORKDIR /app
COPY app.py .
ENV PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8080
CMD ["python", "app.py"]
