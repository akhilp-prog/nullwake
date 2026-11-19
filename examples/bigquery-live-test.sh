#!/bin/bash
# Live BigQuery test for Nullwake, run from Cloud Shell:  bash examples/bigquery-live-test.sh setup | test | defects | cleanup
set -uo pipefail
P=$(gcloud config get-value project 2>/dev/null); VM=nullwake-1; ZONE=asia-south1-a; DS=nw_demo
SA=nullwake-bq-reader; SAE=$SA@$P.iam.gserviceaccount.com
case "${1:-}" in
setup)
  gcloud services enable bigquery.googleapis.com
  bq --location=asia-south1 mk -d $DS 2>/dev/null || true
  bq query -q --use_legacy_sql=false "CREATE OR REPLACE TABLE $DS.orders AS
    SELECT id AS order_id, MOD(id,50)+1 AS customer_id, CAST(10+MOD(id*7,90) AS NUMERIC) AS amount,
           IF(MOD(id,10)=0,'refunded','paid') AS status,
           TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL MOD(id*37,420) MINUTE) AS loaded_at
    FROM UNNEST(GENERATE_ARRAY(1,700)) AS id"
  gcloud iam service-accounts create $SA --display-name "Nullwake BigQuery reader" 2>/dev/null || true
  sleep 5
  for R in roles/bigquery.jobUser roles/bigquery.dataViewer; do
    gcloud projects add-iam-policy-binding $P --member serviceAccount:$SAE --role $R --condition=None -q >/dev/null && echo "granted $R"
  done
  gcloud compute ssh $VM --zone $ZONE --command 'mkdir -p ~/nullwake/bq && chmod 700 ~/nullwake/bq'
  gcloud iam service-accounts keys create /tmp/bq-key.json --iam-account $SAE
  gcloud compute scp /tmp/bq-key.json $VM:~/nullwake/bq/key.json --zone $ZONE && shred -u /tmp/bq-key.json
  echo SETUP-DONE ;;
test)
  sed "s/__PROJECT__/$P/" ~/bigquery-live-test-contracts.yaml > /tmp/bq-contracts.yaml
  gcloud compute scp /tmp/bq-contracts.yaml $VM:~/nullwake/bq/contracts.yaml --zone $ZONE
  gcloud compute ssh $VM --zone $ZONE --command 'sudo docker run --rm -u root -v ~/nullwake/bq:/bq:ro -e BQ_KEY=/bq/key.json \
    --entrypoint nullwake nullwake:0.6.2 check /bq/contracts.yaml --timeout 60'
  echo "exit code $?" ;;
defects)
  bq query -q --use_legacy_sql=false "INSERT INTO $DS.orders (order_id,customer_id,amount,status,loaded_at)
    VALUES (1, 1, NUMERIC '-5', 'paid', CURRENT_TIMESTAMP())" && echo "planted: duplicate order_id 1 with amount -5" ;;
cleanup)
  bq rm -r -f -d $DS
  for R in roles/bigquery.jobUser roles/bigquery.dataViewer; do
    gcloud projects remove-iam-policy-binding $P --member serviceAccount:$SAE --role $R --condition=None -q >/dev/null; done
  gcloud iam service-accounts delete $SAE -q
  gcloud compute ssh $VM --zone $ZONE --command 'rm -rf ~/nullwake/bq' ;;
*) echo "usage: bq.sh setup | test | defects | cleanup"; exit 2;;
esac
