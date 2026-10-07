# Achados do trivy config ignorados nos manifests (make manifests), cada um com
# o motivo.
package trivy

import rego.v1

default ignore := false

# KSV-0109 le o campo pwd do createUser no replica-set.js como senha gravada no
# ConfigMap; o valor e lido do ambiente do initContainer (Secret billing-mongo).
# So este ConfigMap: senha num outro continua reprovando.
ignore if {
	input.ID == "KSV-0109"
	startswith(input.Message, "ConfigMap 'billing-mongo-scripts-")
}
