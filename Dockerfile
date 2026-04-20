FROM python:3.11-slim

WORKDIR /app

# Copy everything
COPY . .

# Install deps from the bot folder
RUN pip install --no-cache-dir -r bot/requirements.txt

# USE THE RIGHT FILENAME HERE!
CMD ["python", "bot/main.py"]
