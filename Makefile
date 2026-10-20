PYTHON ?= python3
REPO := $(CURDIR)

.PHONY: check py-compile ts-check test install doctor plan verify clean

check: py-compile ts-check test

py-compile:
	find packages tools tests -name '*.py' -not -path '*/__pycache__/*' -print0 | xargs -0 -n1 $(PYTHON) -m py_compile

ts-check:
	cd packages/typescript/pi && bun build index.ts --target bun --external @earendil-works/pi-coding-agent --external '@langfuse/*' --external '@opentelemetry/*' --outfile /tmp/connector-pi-check.js
	cd packages/typescript/omp && bun build index.ts --target bun --external @earendil-works/pi-coding-agent --external '@langfuse/*' --external '@opentelemetry/*' --outfile /tmp/connector-omp-check.js
	cd packages/typescript/amp && bun build langfuse.ts --target bun --external @ampcode/plugin --outfile /tmp/connector-amp-check.js
	node --check packages/typescript/codex/plugins/tracing/dist/index.mjs

test:
	$(PYTHON) -m unittest discover -s tests -v

plan:
	$(PYTHON) tools/connector.py plan

install:
	$(PYTHON) tools/connector.py install --all

doctor:
	$(PYTHON) tools/connector.py doctor

verify:
	@test -n "$(HARNESS)" -a -n "$(SESSION)" || (echo "usage: make verify HARNESS=pi SESSION=<id>" && exit 2)
	$(PYTHON) tools/connector.py verify --harness "$(HARNESS)" --session-id "$(SESSION)"

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf '{}' +
	find . -name '.DS_Store' -delete
