# Colmeia — Arquivos Perdidos da História

Infraestrutura local de pesquisa, organização e preservação histórica.

A Colmeia está sendo desenvolvida como uma infraestrutura real, com processamento local, banco de dados, comunicação entre máquinas, execução de missões, pesquisa de fontes, rastreabilidade, auditoria e organização de descobertas.

## Arquitetura atual

- Central / Rainha: máquina principal responsável pela coordenação.
- Worker / Batedora: máquina de processamento responsável pela execução de missões.
- PostgreSQL na Central.
- SQLite operacional no Worker.
- Comunicação autenticada entre Central e Worker.
- Painel web para operação e acompanhamento.

## Funcionalidades já implementadas

- Comunicação real Central ↔ Worker.
- Identificação de Worker e Colmeia.
- Ciclo real de missões.
- Autorização e despacho de missões.
- Execução no Worker.
- Pesquisa histórica real.
- Registro de descobertas.
- Proveniência e rastreabilidade.
- Registro de direitos/licenças.
- Auditoria e eventos.
- Quarentena.
- Favo de organização.
- Detecção e tratamento de duplicidades.
- Catalogação individual através do painel.
- Interface web operacional.

## Princípios

A Colmeia não foi concebida como uma simulação visual.

O objetivo é trabalhar com dados e operações reais, mantendo rastreabilidade das fontes e evitando dados, estados, agentes ou resultados inventados.

A arquitetura também prevê governança pelo Mestre/Apicultor, organização por funções e expansão progressiva da infraestrutura.

## Estado do projeto

Projeto em desenvolvimento ativo.

Novas funções serão implementadas progressivamente sem substituir ou mascarar as operações reais já existentes.

## Estrutura

- `backend/` — núcleo da Central.
- `panel/` — interfaces web.
- `config/` — configuração local, não versionada.
- `data/` — dados operacionais, não versionados.
- `logs/` — registros locais, não versionados.

