FROM python:3.13-alpine

# Set environment variable for timezone
ENV TZ=Europe/Stockholm

# Install dependencies, including tzdata for timezone support
RUN apk add --no-cache tzdata

# Set timezone
RUN cp /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your script
COPY main.py .

# Run script by default when container starts
ENTRYPOINT ["python", "main.py"]