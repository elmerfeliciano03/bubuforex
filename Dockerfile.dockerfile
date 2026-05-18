FROM python:3.14-slim

WORKDIR /app

# Install pre-compiled pandas/numpy from Debian repos
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pandas \
    python3-numpy \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir twelvedata python-telegram-bot requests

COPY forex_signals.py .

CMD ["python", "forex_signals.py"]