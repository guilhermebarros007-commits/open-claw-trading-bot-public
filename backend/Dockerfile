FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Remove unnecessary files
RUN rm -rf __pycache__ app/__pycache__ app/agents/__pycache__ app/tools/__pycache__ app/memory/__pycache__ \
    *.pyc data.db check_*.py debug_*.py test_*.py trigger_*.py verify_*.py \
    static/index_backup*.html static/rewrite_*.py static/test_*.py

EXPOSE 8001

CMD ["python", "run.py"]
