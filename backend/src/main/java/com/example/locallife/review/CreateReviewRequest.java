package com.example.locallife.review;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

import java.util.List;

public record CreateReviewRequest(
        @NotNull(message = "商户编号不能为空")
        @Positive(message = "商户编号必须大于 0")
        Long shopId,

        @NotBlank(message = "评论正文不能为空")
        @Size(max = 2000, message = "评论正文不能超过 2000 个字符")
        String content,

        @NotNull(message = "评论标签不能为空")
        @Size(max = 10, message = "评论标签不能超过 10 个")
        List<@NotBlank(message = "评论标签不能为空") String> tags
) {
}
