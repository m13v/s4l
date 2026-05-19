#!/bin/bash
OUT=/Users/matthewdi/social-autoposter/scripts/podlog_threads_out.txt
> "$OUT"
URLS=(
"https://old.reddit.com/r/selfhosted/comments/1tddgg7/i_want_to_automatically_scrape_my_news_podcasts/"
"https://old.reddit.com/r/podcasting/comments/1sqx0hy/thoughts_on_the_rise_of_ai_generated_podcasts/"
"https://old.reddit.com/r/webdev/comments/1tcnil0/at_what_point_did_web_development_start_feeling/"
"https://old.reddit.com/r/selfhosted/comments/1teic8d/anyone_enjoying_using_ai_to_manage_your_homelab/"
"https://old.reddit.com/r/selfhosted/comments/1tcxb1b/services_with_actually_generous_free_tiers_for/"
"https://old.reddit.com/r/opensource/comments/1t7fx4d/i_contributed_to_open_source_for_the_first_time/"
"https://old.reddit.com/r/opensource/comments/1t5h3j6/how_do_i_start_contributing_to_open_source_devops/"
"https://old.reddit.com/r/opensource/comments/1tfm90j/condenseit_selfhosted_ai_news_digest_mit_licensed/"
"https://old.reddit.com/r/Entrepreneur/comments/1sthfgz/how_do_you_decide_between_code_and_marketing_in/"
)
for URL in "${URLS[@]}"; do
  echo "===URL=== $URL" >> "$OUT"
  TRIES=0
  while [ $TRIES -lt 4 ]; do
    RESP=$(python3 /Users/matthewdi/social-autoposter/scripts/reddit_tools.py fetch "$URL" 2>&1)
    if echo "$RESP" | grep -q '"rate_limited"'; then
      WAIT=$(echo "$RESP" | grep -o '"wait_seconds": *[0-9]*' | grep -o '[0-9]*')
      [ -z "$WAIT" ] && WAIT=300
      sleep $((WAIT + 15))
      TRIES=$((TRIES + 1))
    else
      echo "$RESP" >> "$OUT"
      break
    fi
  done
  sleep 280
done
echo "===DONE===" >> "$OUT"
