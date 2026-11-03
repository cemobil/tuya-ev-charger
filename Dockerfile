FROM python:3.11-slim
WORKDIR /app
COPY app.py icon-192.png icon-512.png ./
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-u", "app.py"]

