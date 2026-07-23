SERVICE_NAME = sub2api
DOCKER_PLATFORM ?= linux/amd64

# Docker 构建上下文 = 仓库根（Dockerfile 在根目录，需要 backend/ frontend/ docs/）
REPO_ROOT ?= ../..
DOCKERFILE ?= $(REPO_ROOT)/Dockerfile

# Baidu BCR / Harbor（与 litellm-relay 同一 registry）
BAIDU_REGISTRY_PUB = ccr-23gxup9u-pub.cnc.bj.baidubce.com
BAIDU_REGISTRY_VPC = ccr-23gxup9u-vpc.cnc.bj.baidubce.com
BAIDU_REPO_NAME = tripo/sub2api
BAIDU_USERNAME ?= root
BAIDU_PASSWORD ?= vastRegistry3d

K8S_NS ?= sub2api
K8S_PULL_SECRET ?= vast-secret
ENV_FILE ?= .env

AUTO_VERSION := $(shell git rev-parse --abbrev-ref HEAD | sed 's/\//-/g')-$(shell date +%Y%m%d%H%M%S)
VERSION ?= $(AUTO_VERSION)

BAIDU_IMAGE_PUB = $(BAIDU_REGISTRY_PUB)/$(BAIDU_REPO_NAME):$(VERSION)
BAIDU_IMAGE_VPC = $(BAIDU_REGISTRY_VPC)/$(BAIDU_REPO_NAME):$(VERSION)
BAIDU_IMAGE_PLACEHOLDER = $(BAIDU_REGISTRY_PUB)/$(BAIDU_REPO_NAME):latest
PRODUCTION_MANIFEST ?= output/manifest-$(VERSION).yaml
K8S_CHECK_MANIFEST = output/manifest-k8s-check.yaml

.PHONY: help baidu-login baidu-docker-build baidu-push baidu-deploy deploy
.PHONY: render-production apply-production restart-production
.PHONY: create-pull-secret create-env-secret show-version k8s-check docker-build-check

.DEFAULT_GOAL := help

help:
	@echo "$(SERVICE_NAME) commands:"
	@echo "  make baidu-login          - login to Baidu Harbor public endpoint"
	@echo "  make baidu-docker-build   - build linux/amd64 image from repo-root Dockerfile"
	@echo "  make baidu-push           - push image to Baidu Harbor public endpoint"
	@echo "  make baidu-deploy         - build + push"
	@echo "  make deploy               - build + push + apply workloads + wait rollout"
	@echo "  make render-production    - render manifest using Baidu VPC image URL"
	@echo "  make apply-production      - apply namespace + render/apply workloads + wait rollout"
	@echo "  make restart-production   - rolling restart"
	@echo "  make create-pull-secret   - create/update imagePullSecret in $(K8S_NS)"
	@echo "  make create-env-secret    - create/update sub2api-env from .env (ENV_FILE=.env)"
	@echo "  make show-version         - print public and VPC image tags"
	@echo "  make k8s-check            - validate Kubernetes manifests (dry-run)"
	@echo "  make docker-build-check   - validate Dockerfile"
	@echo ""
	@echo "Override with VERSION=xxx K8S_NS=xxx when needed."

baidu-login:
	docker login --username $(BAIDU_USERNAME) --password $(BAIDU_PASSWORD) $(BAIDU_REGISTRY_PUB)

baidu-docker-build:
	@echo "--- building $(BAIDU_IMAGE_PUB) ---"
	docker build --platform $(DOCKER_PLATFORM) \
		-f $(DOCKERFILE) \
		-t $(BAIDU_IMAGE_PUB) \
		$(REPO_ROOT)

baidu-push: baidu-login
	docker push $(BAIDU_IMAGE_PUB)

baidu-deploy: baidu-docker-build baidu-push
	@echo "public image:  $(BAIDU_IMAGE_PUB)"
	@echo "cluster image: $(BAIDU_IMAGE_VPC)"

deploy:
	$(MAKE) baidu-deploy VERSION=$(VERSION)
	$(MAKE) apply-production VERSION=$(VERSION)

render-production:
	@mkdir -p $(dir $(PRODUCTION_MANIFEST))
	kubectl kustomize . | \
		sed -e "s|$(BAIDU_IMAGE_PLACEHOLDER)|$(BAIDU_IMAGE_VPC)|g" \
		> $(PRODUCTION_MANIFEST)
	@echo "manifest: $(PRODUCTION_MANIFEST)"
	@echo "cluster image: $(BAIDU_IMAGE_VPC)"

apply-production: render-production
	kubectl apply -f $(PRODUCTION_MANIFEST)
	kubectl rollout status deployment/$(SERVICE_NAME) -n $(K8S_NS) --timeout=300s

restart-production:
	kubectl rollout restart deployment/$(SERVICE_NAME) -n $(K8S_NS)
	kubectl rollout status deployment/$(SERVICE_NAME) -n $(K8S_NS) --timeout=300s

create-pull-secret:
	kubectl -n $(K8S_NS) create secret docker-registry $(K8S_PULL_SECRET) \
		--docker-server=$(BAIDU_REGISTRY_VPC) \
		--docker-username=$(BAIDU_USERNAME) \
		--docker-password=$(BAIDU_PASSWORD) \
		--dry-run=client -o yaml | kubectl apply -f -

create-env-secret:
	@test -f "$(ENV_FILE)" || { echo "set ENV_FILE=<path to .env> (see secret.example.yaml for fields)" >&2; exit 1; }
	kubectl -n $(K8S_NS) create secret generic sub2api-env \
		--from-env-file=$(ENV_FILE) \
		--dry-run=client -o yaml | kubectl apply -f -

show-version:
	@echo "public:  $(BAIDU_IMAGE_PUB)"
	@echo "cluster: $(BAIDU_IMAGE_VPC)"

k8s-check:
	$(MAKE) render-production VERSION=k8s-check PRODUCTION_MANIFEST=$(K8S_CHECK_MANIFEST)
	kubectl create --dry-run=client -f $(K8S_CHECK_MANIFEST) -o name >/dev/null

docker-build-check:
	docker build --check -f $(DOCKERFILE) $(REPO_ROOT)
