NS=qdrant
COL=hybrid_demo
QHOST="qdrant.${NS}.svc.cluster.local:6333"

kubectl -n ${NS} run tmp-client --rm -i --restart=Never --image=curlimages/curl --command -- sh -c '
echo "== create collection =="
cat > /tmp/create.json <<JSON
{
  "vectors": {
    "dense": { "size": 4, "distance": "Cosine" }
  },
  "sparse_vectors": {
    "sparse_bm25": {
      "modifier": "idf",
      "index": { "on_disk": false }
    }
  }
}
JSON
curl -s -X PUT "http://'"${QHOST}"'/collections/'"${COL}"'" \
  -H "Content-Type: application/json" \
  -d @/tmp/create.json

echo
echo "== upsert two points (dense + sparse) =="
cat > /tmp/upsert.json <<JSON
{
  "points": [
    {
      "id": 1,
      "vectors": {
        "dense": [0.1, 0.2, 0.3, 0.4],
        "sparse_bm25": { "indices": [10, 20, 30], "values": [1.0, 1.0, 1.0] }
      },
      "payload": { "text": "cheese and crackers" }
    },
    {
      "id": 2,
      "vectors": {
        "dense": [0.9, 0.8, 0.7, 0.6],
        "sparse_bm25": { "indices": [40, 20, 50], "values": [1.0, 1.0, 1.0] }
      },
      "payload": { "text": "mac and cheese" }
    }
  ]
}
JSON
curl -s -X PUT "http://'"${QHOST}"'/collections/'"${COL}"'/points" \
  -H "Content-Type: application/json" \
  -d @/tmp/upsert.json

echo
echo "== sparse-only query (search token index=20) =="
cat > /tmp/sparse_query.json <<JSON
{
  "query": { "indices": [20], "values": [1.0] },
  "using": "sparse_bm25",
  "limit": 5,
  "with_payload": true
}
JSON
curl -s "http://'"${QHOST}"'/collections/'"${COL}"'/points/query" \
  -H "Content-Type: application/json" \
  -d @/tmp/sparse_query.json

echo
echo "== dense-only query (semantic around vector of point 1) =="
cat > /tmp/dense_query.json <<JSON
{
  "query": [0.1, 0.2, 0.3, 0.4],
  "using": "dense",
  "limit": 5,
  "with_payload": true
}
JSON
curl -s "http://'"${QHOST}"'/collections/'"${COL}"'/points/query" \
  -H "Content-Type: application/json" \
  -d @/tmp/dense_query.json

echo
echo "== hybrid query (sparse prefilter + dense main) =="
cat > /tmp/hybrid.json <<JSON
{
  "prefetch": {
    "prefetch": {
      "query": { "indices": [20], "values": [1.0] },
      "using": "sparse_bm25",
      "limit": 50
    },
    "query": [0.1, 0.2, 0.3, 0.4],
    "using": "dense",
    "limit": 20
  },
  "query": [0.1, 0.2, 0.3, 0.4],
  "using": "dense",
  "limit": 10,
  "with_payload": true
}
JSON
curl -s "http://'"${QHOST}"'/collections/'"${COL}"'/points/query" \
  -H "Content-Type: application/json" \
  -d @/tmp/hybrid.json

echo
echo "== list collections =="
curl -s "http://'"${QHOST}"'/collections"
'


sleep 5

NS=${NS:-qdrant}
COL=${COL:-sparse_ondisk_demo}
QHOST="${QHOST:-qdrant.${NS}.svc.cluster.local:6333}"

echo "== on-disk sparse validation =="

kubectl -n ${NS} exec qdrant-client -- sh -c '
COL=sparse_ondisk_demo
QHOST="qdrant.qdrant.svc.cluster.local:6333"

echo "-> delete collection (ignore error)"
curl -s -X DELETE "http://$QHOST/collections/$COL" || true
sleep 1

echo "-> create sparse on-disk collection"
cat >/tmp/create.json <<EOF
{
  "sparse_vectors": {
    "bm25": {
      "modifier": "idf",
      "index": { "on_disk": true }
    }
  }
}
EOF
curl -s -X PUT "http://$QHOST/collections/$COL" \
  -H "Content-Type: application/json" \
  -d @/tmp/create.json
echo

echo "-> upsert 3 sparse points"
cat >/tmp/upsert.json <<EOF
{
  "points": [
    { "id": 1, "vectors": { "bm25": { "indices": [10], "values": [1.0] } } },
    { "id": 2, "vectors": { "bm25": { "indices": [10], "values": [2.0] } } },
    { "id": 3, "vectors": { "bm25": { "indices": [20], "values": [1.0] } } }
  ]
}
EOF
curl -s -X PUT "http://$QHOST/collections/$COL/points" \
  -H "Content-Type: application/json" \
  -d @/tmp/upsert.json
echo

echo "-> sparse search (expect id 2 first)"
cat >/tmp/query.json <<EOF
{
  "query": { "indices": [10], "values": [1.0] },
  "using": "bm25",
  "limit": 5
}
EOF

curl -s -X POST "http://$QHOST/collections/$COL/points/query" \
  -H "Content-Type: application/json" \
  -d @/tmp/query.json
  
echo
'

echo
echo "== inspect on-disk files (qdrant-0) =="
kubectl -n ${NS} exec qdrant-0 -- sh -c '
COL=sparse_ondisk_demo

echo "-> storage usage"
du -sh /qdrant/storage/collections/$COL 2>/dev/null || echo "no collection"

echo
echo "-> segment file listing"
ls -R /qdrant/storage/collections/$COL 2>/dev/null | sed -n "1,200p" || true
'

echo "== done =="



