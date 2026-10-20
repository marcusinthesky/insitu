//! Bounded, byte-oriented Content-Length framing.
use std::io;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncReadExt, AsyncWrite, AsyncWriteExt};

pub const MAX_MESSAGE_BYTES: usize = 16 * 1024 * 1024;
const MAX_HEADER_BYTES: usize = 8192;

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

/// Read one frame. Clean EOF returns `None`; a partial frame is an error.
pub async fn read_frame<R: AsyncBufRead + Unpin>(reader: &mut R) -> io::Result<Option<Vec<u8>>> {
    let mut headers = Vec::new();
    let mut line = Vec::new();
    let mut length = None;
    loop {
        let available = reader.fill_buf().await?;
        if available.is_empty() {
            return if headers.is_empty() && line.is_empty() {
                Ok(None)
            } else {
                Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "truncated RPC header",
                ))
            };
        }
        let take = available
            .iter()
            .position(|b| *b == b'\n')
            .map_or(available.len(), |i| i + 1);
        if headers.len() + line.len() + take > MAX_HEADER_BYTES {
            return Err(invalid("RPC headers exceed 8 KiB"));
        }
        line.extend_from_slice(&available[..take]);
        reader.consume(take);
        if !line.ends_with(b"\n") {
            continue;
        }
        headers.extend_from_slice(&line);
        if line == b"\n" || line == b"\r\n" {
            break;
        }
        if !line.is_ascii() {
            return Err(invalid("RPC headers must be ASCII"));
        }
        let text = std::str::from_utf8(&line).map_err(|_| invalid("invalid header"))?;
        let (key, value) = text
            .split_once(':')
            .ok_or_else(|| invalid("malformed RPC header"))?;
        if key.trim().eq_ignore_ascii_case("content-length") {
            if length.is_some() {
                return Err(invalid("duplicate Content-Length"));
            }
            let size: usize = value
                .trim()
                .parse()
                .map_err(|_| invalid("invalid Content-Length"))?;
            if size > MAX_MESSAGE_BYTES {
                return Err(invalid("RPC message exceeds 16 MiB"));
            }
            length = Some(size);
        }
        line.clear();
    }
    let mut body = vec![0; length.ok_or_else(|| invalid("missing Content-Length"))?];
    reader.read_exact(&mut body).await?;
    Ok(Some(body))
}

/// Write and flush one UTF-8 JSON payload. Callers serialize concurrent writers.
pub async fn write_frame<W: AsyncWrite + Unpin>(writer: &mut W, payload: &[u8]) -> io::Result<()> {
    if payload.len() > MAX_MESSAGE_BYTES {
        return Err(invalid("RPC message exceeds 16 MiB"));
    }
    writer
        .write_all(format!("Content-Length: {}\r\n\r\n", payload.len()).as_bytes())
        .await?;
    writer.write_all(payload).await?;
    writer.flush().await
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::BufReader;
    #[tokio::test]
    async fn utf8_is_measured_in_bytes() {
        let data = "\"café🦀\"".as_bytes();
        let mut encoded = Vec::new();
        write_frame(&mut encoded, data).await.unwrap();
        let mut reader = BufReader::new(encoded.as_slice());
        assert_eq!(read_frame(&mut reader).await.unwrap().unwrap(), data);
        assert!(read_frame(&mut reader).await.unwrap().is_none());
    }
    #[tokio::test]
    async fn malformed_and_truncated_frames_fail() {
        for value in [
            "Content-Length: -1\r\n\r\n",
            "Content-Length: 999999999\n\n",
            "X: y\n\n",
            "Content-Length: 1\nContent-Length: 1\n\nx",
            "Content-Length: 2\n\nx",
        ] {
            assert!(
                read_frame(&mut BufReader::new(value.as_bytes()))
                    .await
                    .is_err(),
                "{value}"
            );
        }
    }
    #[tokio::test]
    async fn header_budget_is_bounded_without_a_newline() {
        let value = vec![b'x'; MAX_HEADER_BYTES + 1];
        assert!(
            read_frame(&mut BufReader::new(value.as_slice()))
                .await
                .is_err()
        );
    }
}
