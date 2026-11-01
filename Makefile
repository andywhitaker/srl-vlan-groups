PACKAGE := vlan-groups
VERSION := 0.1.0
ARCH := amd64
DEB_PKG := $(PACKAGE)_$(VERSION)_$(ARCH).deb

.PHONY: all package test clean

all: test package

package:
	find app_src -type d -name "__pycache__" -exec rm -rf {} +
	find app_src -type f -name "*.py[co]" -delete
	docker run --rm -v $(CURDIR):/src -w /src goreleaser/nfpm package --config nfpm.yaml --target $(DEB_PKG)

test:
	python3 -m pytest -v

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.py[co]" -delete
	rm -rf .pytest_cache *.deb *.rpm
