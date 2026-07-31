.PHONY: package
package:
	docker run --rm -v $(PWD):/src -w /src goreleaser/nfpm package --config nfpm.yaml --target vlan-groups.deb
	docker run --rm -v $(PWD):/src -w /src goreleaser/nfpm package --config nfpm.yaml --target vlan-groups.rpm
