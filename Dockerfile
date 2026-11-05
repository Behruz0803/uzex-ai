FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build the database at image build time if the CSV data is present.
# (Mount or copy uzex_spot_data/ before building, or mount uzex.db at runtime.)
RUN if [ -d uzex_spot_data ]; then python etl.py; fi

EXPOSE 8000
ENV HOST=0.0.0.0 PORT=8000
CMD ["uvicorn", "webapp.server:app", "--host", "0.0.0.0", "--port", "8000"]
