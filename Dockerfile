FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY prompts/ ./prompts/
COPY user_profile.json .

# Create volume mount point for Obsidian vault
RUN mkdir -p /vault

# Set Python path
ENV PYTHONPATH=/app

# Run the application
CMD ["python", "-m", "src.main"]
