-- “待更新”改为仅由重新抓取后的内容差异产生；清除旧版全量待更新标记。
UPDATE `policy_crawler_article`
SET `content_update_status` = 'not_needed', `content_update_error` = NULL, `content_updated_at` = NULL
WHERE `content_update_status` IN ('pending', 'failed', 'unmatched');
