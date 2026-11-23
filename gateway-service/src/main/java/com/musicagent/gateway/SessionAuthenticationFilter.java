package com.musicagent.gateway;

import java.util.Map;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.cloud.gateway.filter.GlobalFilter;
import org.springframework.cloud.gateway.filter.GatewayFilterChain;
import org.springframework.core.Ordered;
import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.server.ServerWebExchange;
import reactor.core.publisher.Mono;

@Component
public class SessionAuthenticationFilter implements GlobalFilter, Ordered {
    private final WebClient authClient;

    public SessionAuthenticationFilter(WebClient.Builder builder, @Value("${music.auth.url}") String authUrl) {
        this.authClient = builder.baseUrl(authUrl).build();
    }

    @Override
    public int getOrder() { return Ordered.HIGHEST_PRECEDENCE + 1; }

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, GatewayFilterChain chain) {
        String path = exchange.getRequest().getURI().getPath();
        var sanitizedRequest = exchange.getRequest().mutate()
            .headers(headers -> headers.remove("X-Authenticated-User"))
            .build();
        ServerWebExchange sanitizedExchange = exchange.mutate().request(sanitizedRequest).build();
        if (isPublic(path)) return chain.filter(sanitizedExchange);

        String cookie = sanitizedExchange.getRequest().getHeaders().getFirst(HttpHeaders.COOKIE);
        if (cookie == null || cookie.isBlank()) return reject(sanitizedExchange, path);

        Mono<String> authenticatedUserId = authClient.get().uri("/auth/me")
            .header(HttpHeaders.COOKIE, cookie)
            .retrieve()
            .bodyToMono(Map.class)
            .map(body -> body.get("id"))
            .filter(id -> id instanceof String && !((String) id).isBlank())
            .cast(String.class)
            // 认证服务不可用或会话无效时，将请求视为未认证。
            .onErrorResume(error -> Mono.empty());

        return authenticatedUserId
            .flatMap(id -> {
                sanitizedExchange.getAttributes().put("music.authenticatedUserId", id);
                var request = sanitizedExchange.getRequest().mutate()
                    .headers(headers -> {
                        headers.remove("X-Authenticated-User");
                        headers.set("X-Authenticated-User", id);
                    }).build();
                // Mono<Void> 正常完成时本身没有值；先转为非空结果，避免下游 switchIfEmpty 把成功转发误判为认证失败。
                return chain.filter(sanitizedExchange.mutate().request(request).build()).thenReturn(id);
            })
            .switchIfEmpty(Mono.defer(() -> reject(sanitizedExchange, path).then(Mono.<String>empty())))
            .then();
    }

    private boolean isPublic(String path) {
        return path.equals("/") || path.equals("/health") || path.equals("/login") || path.equals("/favicon.ico")
            || path.equals("/robots.txt") || path.equals("/sitemap.xml")
            || path.startsWith("/assets/") || path.startsWith("/auth/");
    }

    private Mono<Void> reject(ServerWebExchange exchange, String path) {
        if (path.equals("/") || path.equals("/app")) {
            exchange.getResponse().setStatusCode(HttpStatus.SEE_OTHER);
            exchange.getResponse().getHeaders().setLocation(java.net.URI.create("/login"));
            return exchange.getResponse().setComplete();
        }
        exchange.getResponse().setStatusCode(HttpStatus.UNAUTHORIZED);
        exchange.getResponse().getHeaders().setContentType(MediaType.APPLICATION_JSON);
        byte[] body = "{\"detail\":\"请先登录。\"}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
        DataBuffer buffer = exchange.getResponse().bufferFactory().wrap(body);
        return exchange.getResponse().writeWith(Mono.just(buffer));
    }
}
