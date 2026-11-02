# Harbor builds verifier and network-policy images inside the task pod.
# The original gc-cli-harbor image has Compose but lacks the Buildx plugin.
FROM registry.mlrnd.ru/docker-proxy/docker:29-cli@sha256:5e6baf67325a79f64ed3bed6863954d5e05d0f3d4e57cbdb14809346814bbfdb AS docker_cli
FROM registry.mlrnd.ru/docker-mlrnd/gc-cli/gc-cli-harbor:v0.3.0_260910@sha256:39072d2b79e8e403910fe9213bed0b3ea4cb0ce271f52f4bd615fe263219fc3e

COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
RUN docker buildx version
