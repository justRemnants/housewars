FROM python:3.10-slim

WORKDIR /app

# Copy everything
COPY . .

# Install your dependencies
# This assumes you have a requirements.txt inside your bot folder
RUN cd bot && pip install --no-cache-dir -r requirements.txt

# Run the python file
CMD ["python", "bot/index.py"]
