export DATABASE_URL="postgresql+psycopg2://soar:soar@localhost:5432/soar"
python evaluate_poc_metrics.py \
  --anomalies *.json\
  -k 5 \
  --timeout 600 \
  --poll 10.0 \
  --ingest-url http://localhost:8001/confirmed-anomalies \
  --out poc_eval_results.csv

export DATABASE_URL="postgresql+psycopg://soar:soar@localhost:5432/soar"                        
python evaluate_poc_metrics.py \
  --anomalies *.json\
  -k 10 \
  --timeout 1800 \
  --poll 30.0 \
  --ingest-url http://localhost:8001/confirmed-anomalies \
  --out poc_eval_results.csv


export DATABASE_URL="postgresql+psycopg://soar:soar@localhost:5432/soar"
python benchmark_models.py \
  --models llama3.2:3b llama3.1:8b qwen2.5:7b-instruct mistral:7b-instruct \
  --anomalies *.json \
  -k 10 \
  --timeout 1800 \
  --poll 10 \
  --ingest-url http://localhost:8001/confirmed-anomalies \
  --db-url postgresql+psycopg://soar:soar@localhost:5432/soar \
  --plots

python benchmark_models.py \
  --models llama3.2:3b \
  --anomalies *.json \
  -k 2 \
  --timeout 1800 \
  --poll 10 \
  --ingest-url http://localhost:8001/confirmed-anomalies \
  --db-url postgresql+psycopg://soar:soar@localhost:5432/soar \
  --plots