FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY pytz-2026.6-py2.py3-none-any.whl /tmp/
RUN pip install --no-index --no-deps --disable-pip-version-check /tmp/pytz-2026.6-py2.py3-none-any.whl
ADD pinned-source.tar.gz /project/
RUN python -m compileall -q /project/sqlglot /project/tests
COPY selector.py semantic.py replay.py impact-index.json.gz semantic-index.json.gz source-reads.json.gz isolated.json.gz module-profile.json.gz /app/
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
COPY frontier.py frontier_child.py frontier_core.py frontier.json.gz memory-profile.json.gz /app/
COPY fixture-attributes.json /app/
COPY output_core.py table-outputs.json.gz /app/
COPY fixture_fields.py /app/
COPY operand_values.py operand-values.json.gz /app/
COPY pure_contract.py helper_replay.py helper-inputs.json.gz /app/
COPY binding_reach.py binding-reads.json.gz /app/
COPY option_flow.py forward-reads.json.gz /app/
COPY import_graph.py import-graph.json.gz /app/
COPY parser_registry.py parser-registry.json.gz /app/
COPY line_gate.py line-gates.json.gz /app/
COPY optimizer_checkpoint.py optimizer_result.py optimizer-packets.zip /app/
COPY syntax-reference.json.gz /app/
COPY private_helper.py /app/
