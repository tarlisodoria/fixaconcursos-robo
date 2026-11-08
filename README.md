# FixaConcursos – robô de ingestão

Robô que alimenta o [FixaConcursos](https://www.fixaconcursos.com.br) com provas e notícias de concursos públicos. Ele roda
aqui, no GitHub Actions, disparado em horários fixos pelo agendador do banco do site (`workflow_dispatch`):

- **vigiar**: lê as listas de concursos e as notícias publicadas pelas bancas;
- **processar**: baixa cadernos e gabaritos oficiais e extrai as questões;
- **enriquecer, classificar, publicar**: indica tópico, assunto e comentário de cada questão e publica o que está completo.

## Fontes e conduta
- Só **sites oficiais das bancas** (Cebraspe, Instituto AOCP, FGV, FCC, ABCP, FADESP e outras), nunca sites de terceiros.
- Respeita o `robots.txt` e os termos de cada site; usa um User-Agent identificado, faz um pedido por vez e pausa entre eles.
  Site bloqueado (Cloudflare, CAPTCHA, `Disallow`) não é contornado: a prova entra por envio manual.
- O gabarito vem sempre do documento oficial da banca.

## Dados
- **Nenhum dado de aluno passa por aqui.** O código só acessa tabelas de conteúdo público (provas, questões, notícias, editais);
  qualquer outra tabela é recusada (`scripts/pipeline/supa.py`, `TABELAS_PUBLICAS`). Avisos aos alunos, planos pedidos e a
  versão do conteúdo do site rodam em outro repositório, privado.
- Chaves e senhas ficam só nos *Secrets* do GitHub; nada de segredo no código.

## Licença
Todos os direitos reservados. Este código não é open source: o repositório é público só para rodar no GitHub Actions.
Não é permitido copiar, modificar ou redistribuir sem autorização por escrito do autor.
