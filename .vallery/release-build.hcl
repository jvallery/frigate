group "vallery-release" {
  targets = ["vallery-standard", "tensorrt"]
}

# The standard image follows the same Dockerfile target used by upstream CI. The TensorRT
# target is imported from docker/tensorrt/trt.hcl by the workflow so its named build contexts
# remain byte-for-byte upstream-compatible.
target "vallery-standard" {
  context    = "."
  dockerfile = "docker/main/Dockerfile"
  target     = "frigate"
  platforms  = ["linux/amd64"]
}
