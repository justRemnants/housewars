FROM python:3.11-slim

WORKDIR /app

# Copy everything (including bot/ and dashboard/ folders)
COPY . .

# Install dependencies from your requirements.txt
# This assumes it's inside the 'bot' folder!
RUN pip install --no-cache-dir -r bot/requirements.txt

# Run the bot using the correct path
CMD ["python", "bot/index.py"]
