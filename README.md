# IG Assessoria: base de prospecção

Gera, uma vez por mês, a base de empresas ativas usada na aba Prospecção do CRM da IG Assessoria,
a partir dos dados abertos do CNPJ publicados pela Receita Federal.

- `scripts/processar_receita.py`: baixa o mês mais recente, filtra os nichos da IG e cifra um arquivo por estado e nicho.
- `.github/workflows/base-receita.yml`: roda sozinho no dia 18 de cada mês e publica o resultado em um release.
- `scripts/chave_publica.pem`: chave usada para cifrar. A chave que abre os arquivos fica só na Netlify do CRM.

Para rodar na hora: aba Actions, "Base da Receita", Run workflow.
