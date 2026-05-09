#!/usr/bin/env sh

set -eu

if [ "$#" -lt 4 ]; then
	printf 'Usage: %s <namespace> <timeout-seconds> <max-retries> <cert> [<cert> ...]\n' "$0" >&2
	exit 1
fi

NAMESPACE=$1
TIMEOUT_SECONDS=$2
MAX_RETRIES=$3
shift 3

KUBECTL_BIN=${KUBECTL_BIN:-kubectl}
SLEEP_SECONDS=${SLEEP_SECONDS:-5}
DEADLINE=$(( $(date +%s) + TIMEOUT_SECONDS ))

latest_order_name() {
	$KUBECTL_BIN get order -n "$NAMESPACE" \
		-o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
	| awk -v prefix="$1-" '$0 ~ ("^" prefix) {name=$0} END {print name}'
}

latest_order_state() {
	$KUBECTL_BIN get order -n "$NAMESPACE" \
		-o jsonpath='{range .items[*]}{.metadata.name} {.status.state}{"\n"}{end}' 2>/dev/null \
	| awk -v prefix="$1-" '$1 ~ ("^" prefix) {state=$2} END {print state}'
}

certificate_ready() {
	$KUBECTL_BIN get certificate "$1" -n "$NAMESPACE" \
		-o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}' 2>/dev/null \
	| awk -F= '$1 == "Ready" { print $2; exit }'
}

cleanup_failed_issuance() {
	cert_name=$1
	stale_resources=$(
		$KUBECTL_BIN get certificaterequest,order,challenge,secret -n "$NAMESPACE" -o name 2>/dev/null \
		| grep "/${cert_name}-" || true
	)

	if [ -n "$stale_resources" ]; then
		printf 'Removing stale cert-manager resources for %s\n' "$cert_name"
		printf '%s\n' "$stale_resources" | while IFS= read -r resource; do
			[ -n "$resource" ] || continue
			$KUBECTL_BIN delete -n "$NAMESPACE" --ignore-not-found "$resource" >/dev/null
		done
	fi

	$KUBECTL_BIN delete certificate "$cert_name" -n "$NAMESPACE" --ignore-not-found >/dev/null
}

for cert_name in "$@"; do
	retries=0
	last_state=""

	printf 'Waiting for certificate %s\n' "$cert_name"

	while :; do
		now=$(date +%s)
		if [ "$now" -ge "$DEADLINE" ]; then
			printf 'Timed out waiting for certificate %s after %ss\n' "$cert_name" "$TIMEOUT_SECONDS" >&2
			exit 1
		fi

		if [ "$(certificate_ready "$cert_name" || true)" = "True" ]; then
			printf 'Certificate %s is ready\n' "$cert_name"
			break
		fi

		order_name=$(latest_order_name "$cert_name")
		order_state=$(latest_order_state "$cert_name")

		if [ "$order_state" != "$last_state" ]; then
			if [ -n "$order_state" ]; then
				printf 'Certificate %s currently has order %s in state %s\n' "$cert_name" "$order_name" "$order_state"
			else
				printf 'Certificate %s has not created an ACME order yet\n' "$cert_name"
			fi
			last_state=$order_state
		fi

		if [ "$order_state" = "errored" ]; then
			retries=$((retries + 1))
			if [ "$retries" -gt "$MAX_RETRIES" ]; then
				printf 'Certificate %s exceeded retry budget after repeated errored orders\n' "$cert_name" >&2
				exit 1
			fi

			printf 'Recreating failed certificate issuance for %s (%s/%s)\n' "$cert_name" "$retries" "$MAX_RETRIES"
			cleanup_failed_issuance "$cert_name"
			last_state=""
		fi

		sleep "$SLEEP_SECONDS"
	done
done
