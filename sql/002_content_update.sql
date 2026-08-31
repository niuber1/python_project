-- 历史记录的 KMS 正文缺少标题/发文日期/文号，需要用户在“待更新”页确认后覆盖更新。
ALTER TABLE `policy_crawler_article`
  ADD COLUMN `content_update_status` varchar(32) NOT NULL DEFAULT 'pending' COMMENT '正文覆盖更新状态' AFTER `kms_status`,
  ADD COLUMN `content_update_error` text COMMENT '正文覆盖更新错误摘要' AFTER `content_update_status`,
  ADD COLUMN `content_updated_at` datetime DEFAULT NULL COMMENT '正文覆盖更新时间' AFTER `content_update_error`,
  ADD KEY `idx_policy_crawler_content_update_status` (`content_update_status`,`updated_at`);
