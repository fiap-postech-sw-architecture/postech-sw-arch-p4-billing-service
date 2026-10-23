# Mutação

> [↑ README](../README.md#mutação)

Cobertura diz que a linha rodou; mutação diz se a suíte percebe quando ela está errada (ADR-041). Não é gate de CI e não tem pontuação mínima.

O `mutmut` não roda neste repositório. A versão 3.8.0 espera o pacote em `src/<pacote>` e tira o prefixo `src.` do nome do módulo, e o pacote do serviço se chama `src`: a primeira função mutada que os testes chamam reprova com

```text
AssertionError: Failed trampoline hit. Module name starts with `src.`, which is invalid
```

(`mutmut/stats.py`, `record_trampoline_hit`). Renomear o pacote ou remendar a ferramenta mudaria o que se mede, então a alternativa são mutantes escritos à mão: [`scripts/mutantes.toml`](../scripts/mutantes.toml) lista 76 defeitos plausíveis nos módulos críticos, e [`scripts/mutacao_manual.py`](../scripts/mutacao_manual.py) aplica um por vez numa cópia do código (`mutants/`, fora do git) e roda só os testes que deveriam pegá-lo.

**Resultado** em 23/10/2026: 74 mortos de 74 medidos (100%) em 4,8 min (cerca de 5 min com a máquina carregada), com 2 equivalentes de fora da conta.

| Área | Mutantes | Mortos | Equivalentes |
|---|---:|---:|---:|
| `Dinheiro` | 8 | 7 | 1 |
| Status do orçamento | 9 | 9 | 0 |
| Status do pagamento | 10 | 10 | 0 |
| Validação do JWT | 14 | 13 | 1 |
| Idempotência | 7 | 7 | 0 |
| Consumidor | 5 | 5 | 0 |
| Relay | 9 | 9 | 0 |
| Compensação da saga | 5 | 5 | 0 |
| Observabilidade | 9 | 9 | 0 |
| **total** | **76** | **74** | **2** |

- **Limites que só a mutação mostrou, hoje com teste:**
  - moeda de 4 letras aceita (`test_moeda_fora_do_iso_4217_e_rejeitada`, caso `quatro-letras`);
  - cópia do JWKS ainda valendo na hora exata do limite de 1 h (`test_copia_velha_vale_ate_1_hora_quando_a_renovacao_falha`);
  - corpo no teto de 64 KiB tratado como grande demais (`test_corpo_no_teto_exato_ainda_passa_do_limite_de_tamanho`);
  - expiração de pagamento que a outra escrita já tirou de `SOLICITADO` gerando log de erro (`test_pagamento_que_deixou_de_estar_vencido_nao_vira_erro`);
  - leeway de 10 s do `exp` do JWT (J12): `test_leeway_de_10_segundos_no_exp` cerca o valor entre 9 e 12 s e `test_leeway_e_o_do_contrato_com_o_os_service` o fixa;
  - arredondamento para cima do `Retry-After` do 503 do JWKS (J13) e dos segundos até a próxima prova do disjuntor (J14), ambos com `ceil` virando `floor`: `test_retry_after_arredonda_para_cima_o_que_falta_da_memoria_da_falha` e `test_segundos_para_nova_tentativa_arredonda_para_cima` usam espera fracionária, porque com espera inteira os dois arredondamentos dão o mesmo número.
- **Equivalentes**, cada um com a condição em que vale no campo `equivalente` do `mutantes.toml`:
  - D01, o teto exato do `Dinheiro` (`>=` virando `>`): só para +10^10, que a checagem seguinte, depois do arredondamento, recusa com a mesma mensagem. Para -10^10 a recusa é a mesma exceção, mas a mensagem muda, de "acima de 10 dígitos inteiros" para "Valor nao pode ser negativo", e nenhum teste confere a mensagem desse caso.
  - J01, `HS256` na lista de algoritmos do JWKS: equivale no resultado (o token segue dando 401, só muda o texto do erro do PyJWT, que a API não usa), e só enquanto valem duas condições: o PyJWT amarra a chave ao algoritmo dela (na 2.15, o `decode` com um `PyJWK` recusa `Token algorithm 'HS256' does not match the key's algorithm 'RS256'`) e o validador passa um `PyJWK` ao `decode`. Sem elas, a lista `RS256` seria a única barreira, e nenhum teste a prende.
- **O que o percentual não é:** a lista é curada, então ele diz que a suíte pega esses 76 defeitos, e não é comparável ao de uma ferramenta que gera os mutantes sozinha.

Para refazer:

```bash
make mutacao
make mutacao MUTANTES="D06 R03"
```

Como o alvo se comporta:

- Para de iniciar mutantes depois de `MUTACAO_PRAZO` segundos (900); os que sobram saem `nao rodado`. A tabela vai para `reports/mutacao.md`.
- Antes do primeiro mutante de cada grupo de testes, roda o grupo na cópia sem mutante. Se o grupo já reprova ou estoura o tempo, os mutantes dele saem `erro`: um teste que já falha mataria qualquer um.
- Com `erro` ou `nao rodado`, o percentual vira `n/d`.
- Os testes de integração sobem um MongoDB cada; com `TEST_MONGODB_URI` apontando para um replica set no ar, poupam o container. A exceção são os mutantes OBS04 a OBS07 (o coletor da outbox): `TestPrazosDoColetor` sobe sempre um `mongod` próprio, com os comandos de teste ligados, e ignora a `TEST_MONGODB_URI`; sem Docker esses quatro saem `erro`.
- O script lê o arquivo original de `src/` a cada mutante: não edite `src/` com a bateria rodando.
- Um mutante novo entra com o trecho exato (`de`, uma ocorrência no arquivo), e `tests/unitarios/test_mutacao_manual.py` reprova se a lista envelhecer.

Códigos de saída do script (`scripts/mutacao_manual.py`):

- `0`: nenhum mutante saiu `erro`. Sobrevivente e `nao rodado` não reprovam, porque não há gate (ADR-041).
- `1`: algum mutante saiu `erro`, uma medida inválida, como o Docker fora.
- `2`: um id de `MUTANTES` não existe (a mensagem lista os válidos) ou o `.toml` não bate com o código.

Esses são os códigos do script, e o `make mutacao` não os repassa: com falha, ele mostra `Error 1` ou `Error 2` e sai sempre com 2 (medido com um id desconhecido, com `PY=false` e com um mutante em `erro`); sem falha, sai com 0.

**Para outro serviço:** copie `scripts/mutacao_manual.py` e o alvo `mutacao` do Makefile para o repositório dele (o script é o mesmo nos três serviços). Troque a tabela `[config]` do `scripts/mutantes.toml` (o que vai para a cópia `mutants/`), os grupos de `[testes]` e os `[[mutante]]`. Antes de começar, o script recusa trecho ausente ou repetido.
