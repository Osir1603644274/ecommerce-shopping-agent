package com.example.locallife.review;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

import java.util.List;

public record UpdateReviewRequest(
        @NotBlank(message = "评论正文不能为空")
        @Size(max = 2000, message = "评论正文不能超过 2000 个字符")
        String content,

        @NotNull(message = "评论标签不能为空")
        @Size(max = 10, message = "评论标签不能超过 10 个")
        List<@NotBlank(message = "评论标签不能为空") String> tags
) {
}
