# news-pool

Collects Korean news headlines via the NAVER news search API and Google News RSS,
then writes them to plain text files under `pool/`.

Search terms are supplied at runtime through the `QUERIES_JSON` environment
variable and are not stored in this repository. See `config/queries.sample.json`
for the expected shape.

Output files contain only publicly available headline metadata:
publication time, outlet, headline, and link.

```
pool/set-a.txt
pool/set-b.txt
pool/set-c.txt
pool/index.txt
```
