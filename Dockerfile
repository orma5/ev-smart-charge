FROM python:3.13-slim

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your script
COPY main.py .

# Run script by default when container starts
ENTRYPOINT ["python", "main.py"]