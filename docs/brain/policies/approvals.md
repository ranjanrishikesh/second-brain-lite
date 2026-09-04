# Approvals

Web research requires explicit approval before acquisition. Record and retain approved evidence with its provenance.

Knowledge operations must distinguish a request to inspect existing retained evidence from a request to acquire new evidence. When approval is absent, do not perform the acquisition step.

Source-version adoption is also an explicit approval boundary. Before invoking `brain source adopt-version`, the calling agent must obtain approval for the exact observed candidate checksum and provide a nonempty factual approval note. The command does not infer approval from a modified working tree or from the presence of the note itself. Adoption remains blocked when the live candidate changed again, any prior exact version cannot be recovered, an existing archive disagrees with its retained checksum, archive durability cannot be completed, or the final archive-and-live snapshot cannot be verified safely. Treat the adoption as committed only when the handler proves the exact adopted record at the freshly opened canonical shard. A warning after that proof does not revoke the adoption: preserve the returned citation rewrites and perform the stated summary or durability recovery action. A detached or unproved publication returns no rewrite claim.
