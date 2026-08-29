
# MAC-Coletor 1.0

Protótipo mobile do coletor de dados para o MAC 2.0.

## O que esta versão faz
- Roda online em Streamlit.
- Coleta 5 jogadores de validação:
  - Samuel Lino
  - Luciano Juba
  - Canobbio
  - Gabriel
  - Marcelinho
- Extrai nome, clube, posição, média geral, média casa, média fora.
- Extrai histórico por rodada, contexto casa/fora e scouts.
- Gera um único JSON para envio ao MAC.
- Regra obrigatória: ausência na fonte não vira zero. Usa `pontos: null` e `status: "sem_registro"`.

## Arquivos
- `app.py` — aplicativo.
- `requirements.txt` — dependências.

## Publicar no Streamlit Community Cloud
1. Crie um repositório no GitHub.
2. Envie `app.py` e `requirements.txt`.
3. No Streamlit Community Cloud, escolha esse repositório.
4. Defina `app.py` como arquivo principal.
5. Publique e abra o link pelo celular.

## Próxima versão
Depois da validação do protótipo:
- descobrir automaticamente todos os jogadores da rodada;
- filtros por posição/status;
- selecionar apenas prováveis;
- gerar o JSON completo do mercado;
- adicionar auditoria de coleta.
