# French green destination poles, 2025

`fr_green_poles_2025.json` contains 365 entries from the French pole inventory
published with the 23 April 2025 order, Journal officiel no. 104 of 3 May 2025.

Official annex (primary reference):
https://www.sdnsd.developpement-durable.gouv.fr/IMG/pdf/joe_20250503_0104_0020-2.pdf

Machine-readable transcription by Etienne Zind, published on data.gouv.fr under
CC0 1.0:
https://www.data.gouv.fr/datasets/liste-des-poles-verts-selon-le-journal-officiel-ndeg104-du-3-mai-2025

Input CSV:
https://static.data.gouv.fr/resources/liste-des-poles-verts-selon-le-journal-officiel-ndeg104-du-3-mai-2025/20261016-122953/poles-verts-fr.csv

SHA-256 of input bytes:
`96bcf5bc78b6480571b64075fa9c7e9b0cce659ed21247fa565b0d50db4a0bc6`

The local JSON preserves the CSV name, class and department fields. Bipoles use
`|` between names. Runtime matching folds accents, expands St/Ste, and recognises
the Clermont-FD abbreviation; it does not guess qualified destinations such as
"Bourges-Centre". No external data download is needed at runtime.

This is a fallback for French exit registers without explicit colour metadata,
not a complete physical-sign or green-itinerary inventory. Administrative status
alone does not determine the colour. Explicit mapped/reviewed registers always
take precedence, and supplied destination names remain authoritative.
