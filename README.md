# Dados da app Benfica Independente

Robô que corre sozinho de 30 em 30 minutos e mantém atualizados, na pasta `data/`,
a agenda, os resultados e a arrumação dos vídeos por programa usados pela app.

- Sem chave de IA: o robô faz tudo sozinho.
- Com o segredo `GEMINI_API_KEY` (Settings → Secrets and variables → Actions):
  o agente Gemini ajuda quando o formato das páginas muda e arruma os vídeos novos
  em programas. Se a chave falhar ou acabarem os pedidos grátis do dia, o robô continua.
